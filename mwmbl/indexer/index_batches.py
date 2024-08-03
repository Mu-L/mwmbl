"""
Index batches that are stored locally.
"""
import math
from collections import defaultdict
from datetime import datetime
from functools import reduce
from logging import getLogger
from typing import Collection, Iterable

from mwmbl.crawler.batch import HashedBatch, Item
from mwmbl.crawler.urls import URLStatus
from mwmbl.indexer import process_batch
from mwmbl.indexer.batch_cache import BatchCache
from mwmbl.indexer.index import tokenize_document
from mwmbl.indexer.indexdb import BatchStatus
from mwmbl.tinysearchengine.indexer import Document, TinyIndex
from mwmbl.tinysearchengine.rank import score_result, DOCUMENT_FREQUENCIES, N_DOCUMENTS, HeuristicRanker
from mwmbl.utils import add_term_infos

logger = getLogger(__name__)


def get_documents_from_batches(batches: Collection[HashedBatch]) -> Iterable[tuple[str, str, str]]:
    for batch in batches:
        for item in batch.items:
            if item.content is not None and not item.content.links_only:
                yield item.content.title, item.url, item.content.extract


def run(batch_cache: BatchCache, index_path: str):

    def process(batches: Collection[HashedBatch]):
        index_batches(batches, index_path)
        logger.info("Indexed pages")

    process_batch.run(batch_cache, BatchStatus.URLS_UPDATED, BatchStatus.INDEXED, process, 10000)


def get_url_score(url):
    # TODO: compute a proper score for each document
    return 1/len(url)


def index_batches(batch_data: Collection[HashedBatch], index_path: str):
    start_time = datetime.utcnow()
    document_tuples = list(get_documents_from_batches(batch_data))
    documents = [Document(title, url, extract) for title, url, extract in document_tuples]
    page_documents = preprocess_documents(documents, index_path)
    index_pages(index_path, page_documents)
    end_time = datetime.utcnow()
    logger.info(f"Indexing took {end_time - start_time}")


def index_pages(index_path: str, page_documents: dict[int, list[Document]]):
    with TinyIndex(Document, index_path, 'w') as indexer:
        ranker = HeuristicRanker(indexer, None, score_threshold=float('-inf'))
        for page, documents in page_documents.items():
            new_documents = []
            existing_documents = indexer.get_page(page)
            seen_urls = set()
            seen_titles = set()

            sorted_documents = sort_documents(documents, existing_documents, ranker)

            for document in sorted_documents:
                if document.title in seen_titles or document.url in seen_urls:
                    continue
                new_documents.append(document)
                seen_urls.add(document.url)
                seen_titles.add(document.title)
            logger.info(f"Storing {len(new_documents)} documents for page {page}, originally {len(existing_documents)}")
            indexer.store_in_page(page, new_documents)


def sort_documents(documents, all_existing_documents, ranker):
    curated_documents = [doc for doc in all_existing_documents if doc.state is not None]
    existing_documents = [doc for doc in all_existing_documents if doc.state is None]

    term_documents = defaultdict(list)

    for document in documents:
        if document.term is not None:
            term_documents[document.term].append(document)

    ordered_term_docs = defaultdict(list)
    for term, docs in term_documents.items():
        docs += [doc for doc in existing_documents if doc.term == term]
        ordered_docs = ranker.order_results(term.split(), docs, True)
        ordered_term_docs[term] = ordered_docs

    # Existing docs are already ordered
    other_terms = {doc.term for doc in existing_documents if doc.term not in ordered_term_docs}
    for doc in existing_documents:
        if doc.term in other_terms:
            ordered_term_docs[doc.term].append(doc)

    numbered_docs = [enumerate(docs) for docs in ordered_term_docs.values()]
    combined_docs = [doc for docs in numbered_docs for doc in docs]
    indexes, sorted_documents = zip(*sorted(combined_docs, key=lambda x: x[0]))
    return curated_documents + list(sorted_documents)


def preprocess_documents(documents, index_path):
    page_documents = defaultdict(list)
    with TinyIndex(Document, index_path, 'r') as indexer:
        for i, document in enumerate(documents):
            if i % 1000 == 0:
                logger.info(f"Preprocessing document {i} of {len(documents)}")

            tokenized = tokenize_document(document.url, document.title, document.extract, document.score)
            for token in tokenized.tokens:
                page = indexer.get_key_page_index(token)
                term_document = Document(document.title, document.url, document.extract, term=token)
                page_documents[page].append(term_document)
    print(f"Preprocessed for {len(page_documents)} pages")
    return page_documents


def get_url_error_status(item: Item):
    if item.status == 404:
        return URLStatus.ERROR_404
    if item.error is not None:
        if item.error.name == 'AbortError':
            return URLStatus.ERROR_TIMEOUT
        elif item.error.name == 'RobotsDenied':
            return URLStatus.ERROR_ROBOTS_DENIED
    return URLStatus.ERROR_OTHER
