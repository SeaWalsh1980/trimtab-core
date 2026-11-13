"""GhApi reads response headers and follows a Link-header cursor (ADR 0063 E13, ENG-6.2)."""

import json
import unittest

from gh_fake import FakeGh, query_of
from trimtab.capture.ghapi import GhApi, SourceError

ACTIVITY = "repos/o/r/activity"


def reply(body, link=None, status="HTTP/2.0 200 OK"):
    head = [status, "Content-Type: application/json"]
    if link:
        head.append(f"Link: {link}")
    return "\r\n".join(head) + "\r\n\r\n" + json.dumps(body)


def next_link(cursor):
    return f'<https://api.github.com/repos/o/r/activity?per_page=2&after={cursor}>; rel="next"'


class HeadersTest(unittest.TestCase):
    def test_headers_are_returned_with_the_body(self):
        gh = FakeGh({ACTIVITY: reply([1], link=next_link("abc"))})
        headers, body = GhApi(gh).call_with_headers("GET", ACTIVITY)
        self.assertEqual(headers["link"], next_link("abc"))
        self.assertEqual(json.loads(body), [1])

    def test_the_include_flag_is_sent(self):
        gh = FakeGh({ACTIVITY: reply([])})
        GhApi(gh).call_with_headers("GET", ACTIVITY)
        self.assertIn("-i", gh.argvs()[0])

    def test_a_response_with_no_header_block_is_an_error(self):
        gh = FakeGh({ACTIVITY: "[]"})
        with self.assertRaises(SourceError):
            GhApi(gh).call_with_headers("GET", ACTIVITY)


class CursorPagesTest(unittest.TestCase):
    def test_two_pages_are_joined_by_the_cursor_never_by_page_number(self):
        gh = FakeGh({ACTIVITY: [reply([1, 2], link=next_link("c1")), reply([3])]})
        rows = GhApi(gh).cursor_pages(ACTIVITY, {"ref": "refs/heads/x"}, page_limit=5, per_page=2, what="activity")
        self.assertEqual(rows, [1, 2, 3])
        queries = [query_of(a) for a in gh.argvs()]
        self.assertNotIn("after", queries[0])
        self.assertEqual(queries[1]["after"], "c1")
        self.assertTrue(all("page" not in q for q in queries))

    def test_reaching_the_page_limit_with_more_to_read_is_an_error(self):
        gh = FakeGh({ACTIVITY: [reply([1, 2], link=next_link("c1")), reply([3, 4], link=next_link("c2"))]})
        with self.assertRaises(SourceError):
            GhApi(gh).cursor_pages(ACTIVITY, {}, page_limit=2, per_page=2, what="activity")

    def test_exactly_the_limit_with_no_next_link_is_complete(self):
        gh = FakeGh({ACTIVITY: [reply([1, 2], link=next_link("c1")), reply([3, 4])]})
        rows = GhApi(gh).cursor_pages(ACTIVITY, {}, page_limit=2, per_page=2, what="activity")
        self.assertEqual(rows, [1, 2, 3, 4])

    def test_a_non_list_page_is_an_error(self):
        gh = FakeGh({ACTIVITY: reply({"message": "x"})})
        with self.assertRaises(SourceError):
            GhApi(gh).cursor_pages(ACTIVITY, {}, page_limit=2, per_page=2, what="activity")

    def test_a_next_link_with_no_cursor_is_an_error(self):
        link = '<https://api.github.com/repos/o/r/activity?page=2>; rel="next"'
        gh = FakeGh({ACTIVITY: [reply([1], link=link)]})
        with self.assertRaises(SourceError):
            GhApi(gh).cursor_pages(ACTIVITY, {}, page_limit=3, per_page=2, what="activity")

    def test_a_next_link_after_a_prev_link_is_followed(self):
        link = ('<https://api.github.com/repos/o/r/activity?before=b0>; rel="prev", '
                '<https://api.github.com/repos/o/r/activity?after=c9>; rel="next"')
        gh = FakeGh({ACTIVITY: [reply([1], link=link), reply([2])]})
        self.assertEqual(GhApi(gh).cursor_pages(ACTIVITY, {}, page_limit=3, per_page=2, what="activity"), [1, 2])
        self.assertEqual(query_of(gh.argvs()[1])["after"], "c9")

    def test_a_next_entry_with_rel_after_other_parameters_is_followed(self):
        link = '<https://api.github.com/repos/o/r/activity?after=c9>; title="x"; rel="next"'
        gh = FakeGh({ACTIVITY: [reply([1], link=link), reply([2])]})
        self.assertEqual(GhApi(gh).cursor_pages(ACTIVITY, {}, page_limit=3, per_page=2, what="activity"), [1, 2])

    def test_a_link_header_that_says_next_in_a_form_not_read_is_an_error(self):
        gh = FakeGh({ACTIVITY: [reply([1], link="rel=next;<https://api.github.com/x?after=c9>")]})
        with self.assertRaises(SourceError):
            GhApi(gh).cursor_pages(ACTIVITY, {}, page_limit=3, per_page=2, what="activity")

    def test_a_repeated_link_header_is_read_whole(self):
        raw = ("HTTP/2.0 200 OK\r\nLink: <https://api.github.com/x?before=b>; rel=\"prev\"\r\n"
               "Link: <https://api.github.com/x?after=c9>; rel=\"next\"\r\n\r\n[1]")
        gh = FakeGh({ACTIVITY: [raw, reply([2])]})
        self.assertEqual(GhApi(gh).cursor_pages(ACTIVITY, {}, page_limit=3, per_page=2, what="activity"), [1, 2])

    def test_a_last_page_whose_urls_say_next_in_a_repository_name_is_the_last_page(self):
        link = '<https://api.github.com/repos/o/nextjs-shop/activity?before=b0>; rel="prev"'
        gh = FakeGh({ACTIVITY: [reply([1], link=link)]})
        self.assertEqual(GhApi(gh).cursor_pages(ACTIVITY, {}, page_limit=3, per_page=2, what="activity"), [1])

    def test_an_upper_case_rel_is_followed(self):
        link = '<https://api.github.com/repos/o/r/activity?after=c9>; rel="Next"'
        gh = FakeGh({ACTIVITY: [reply([1], link=link), reply([2])]})
        self.assertEqual(GhApi(gh).cursor_pages(ACTIVITY, {}, page_limit=3, per_page=2, what="activity"), [1, 2])

    def test_a_page_limit_below_one_or_a_caller_set_cursor_is_an_error(self):
        gh = FakeGh({ACTIVITY: reply([])})
        for kwargs in ({"query": {}, "page_limit": 0}, {"query": {"page": 2}, "page_limit": 2},
                       {"query": {"after": "x"}, "page_limit": 2}):
            with self.subTest(kwargs), self.assertRaises(SourceError):
                GhApi(gh).cursor_pages(ACTIVITY, kwargs["query"], page_limit=kwargs["page_limit"], per_page=2,
                                       what="activity")
        self.assertEqual(gh.calls, [])

    def test_a_failed_read_is_an_error(self):
        gh = FakeGh({ACTIVITY: reply([])}, returncode=1, stderr="HTTP 500")
        with self.assertRaises(SourceError):
            GhApi(gh).cursor_pages(ACTIVITY, {}, page_limit=2, per_page=2, what="activity")


if __name__ == "__main__":
    unittest.main()
