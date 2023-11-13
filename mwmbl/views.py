from dataclasses import dataclass
from datetime import datetime
from itertools import groupby
from urllib.parse import urlparse, parse_qs

import justext
import requests
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django_htmx.http import push_url

from mwmbl.format import format_result
from mwmbl.models import UserCuration, MwmblUser
from mwmbl.search_setup import ranker

from justext.core import html_to_dom, ParagraphMaker, classify_paragraphs, revise_paragraph_classification, \
    LENGTH_LOW_DEFAULT, STOPWORDS_LOW_DEFAULT, MAX_LINK_DENSITY_DEFAULT, NO_HEADINGS_DEFAULT, LENGTH_HIGH_DEFAULT, \
    STOPWORDS_HIGH_DEFAULT, MAX_HEADING_DISTANCE_DEFAULT, DEFAULT_ENCODING, DEFAULT_ENC_ERRORS, preprocessor

from mwmbl.settings import NUM_EXTRACT_CHARS
from mwmbl.tinysearchengine.indexer import Document
from django.conf import settings


def justext_with_dom(html_text, stoplist, length_low=LENGTH_LOW_DEFAULT,
        length_high=LENGTH_HIGH_DEFAULT, stopwords_low=STOPWORDS_LOW_DEFAULT,
        stopwords_high=STOPWORDS_HIGH_DEFAULT, max_link_density=MAX_LINK_DENSITY_DEFAULT,
        max_heading_distance=MAX_HEADING_DISTANCE_DEFAULT, no_headings=NO_HEADINGS_DEFAULT,
        encoding=None, default_encoding=DEFAULT_ENCODING,
        enc_errors=DEFAULT_ENC_ERRORS):
    """
    Converts an HTML page into a list of classified paragraphs. Each paragraph
    is represented as instance of class ˙˙justext.paragraph.Paragraph˙˙.
    """
    dom = html_to_dom(html_text, default_encoding, encoding, enc_errors)

    titles = dom.xpath("//title")
    title = titles[0].text if len(titles) > 0 else None

    dom = preprocessor(dom)

    paragraphs = ParagraphMaker.make_paragraphs(dom)

    classify_paragraphs(paragraphs, stoplist, length_low, length_high,
        stopwords_low, stopwords_high, max_link_density, no_headings)
    revise_paragraph_classification(paragraphs, max_heading_distance)

    return paragraphs, title


def index(request):
    activity, query, results = _get_results_and_activity(request)
    return render(request, "index.html", {
        "results": results,
        "query": query,
        "user": request.user,
        "activity": activity,
        "footer_links": settings.FOOTER_LINKS,
    })


def home_fragment(request):
    activity, query, results = _get_results_and_activity(request)
    response = render(request, "home.html", {
        "results": results,
        "query": query,
        "activity": activity,
    })
    current_url = request.htmx.current_url
    # Replace query string with new query
    stripped_url = current_url[:current_url.index("?")] if "?" in current_url else current_url
    query_string = "?q=" + query if len(query) > 0 else ""
    new_url = stripped_url + query_string
    # Set the htmx replace header
    response["HX-Replace-Url"] = new_url
    return response


@dataclass
class Activity:
    user: MwmblUser
    num_curations: int
    timestamp: datetime
    query: str
    url: str


def _get_results_and_activity(request):
    query = request.GET.get("q")
    if query:
        results = ranker.search(query)
        activity = None
    else:
        results = None
        curations = UserCuration.objects.order_by("-timestamp")[:100]
        sorted_curations = sorted(curations, key=lambda x: x.user.username)
        groups = groupby(sorted_curations, key=lambda x: (x.user.username, x.url))
        unsorted_activity = []
        for (user, url), group in groups:
            parsed_url_query = parse_qs(urlparse(url).query)
            activity_query = parsed_url_query.get("q", [""])[0]
            group = list(group)
            unsorted_activity.append(Activity(
                user=user,
                num_curations=len(group),
                timestamp=max([i.timestamp for i in group]),
                query=activity_query,
                url=url,
            ))

        activity = sorted(unsorted_activity, key=lambda a: a.timestamp, reverse=True)
    return activity, query, results


def fetch_url(request):
    url = request.GET["url"]
    query = request.GET["query"]
    response = requests.get(url)
    paragraphs, title = justext_with_dom(response.content, justext.get_stoplist("English"))
    good_paragraphs = [p for p in paragraphs if p.class_type == 'good']

    extract = ' '.join([p.text for p in good_paragraphs])
    if len(extract) > NUM_EXTRACT_CHARS:
        extract = extract[:NUM_EXTRACT_CHARS - 1] + '…'

    result = Document(title=title, url=url, extract=extract, score=0.0)
    return render(request, "result.html", {
        "result": format_result(result, query),
    })


def page_history(request):
    url = request.GET["url"]
    parsed_url_query = parse_qs(urlparse(url).query)
    query = parsed_url_query.get("q", [""])[0]
    curations = UserCuration.objects.filter(url=url).order_by("-timestamp")
    return render(request, "history.html", {
        "curations": curations,
        "url": url,
        "query": query,
    })
