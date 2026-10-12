"""GitHub through `gh api` on REST paths only: cloud sessions refuse GraphQL, and gh's porcelain uses it."""

import json
import unittest

from gh_fake import FakeGh, method_of, path_of, query_of
from trimtab.capture.sources import (
    SEARCH_RESULT_CAP, GhApi, GhContents, GhIssues, GhPullRequests, GhSource, SourceError, open_feedback_issues,
)

REPO = "owner/repo"
LABEL = "trimtab-ingested"
FEEDBACK = "harness-feedback"
SINCE = "2026-09-01"
PR = 76
RUN_ID = 1001
HEAD_SHA = "a" * 40
WORKFLOW = "ci.yml"
ISSUE_URL = "https://github.com/owner/repo/issues/9"


def search_item(number, body="", merged_at="2026-10-01T12:00:00Z"):
    return {"number": number, "body": body, "pull_request": {"merged_at": merged_at}}


def search_page(*items, incomplete=False):
    return json.dumps({"total_count": len(items), "incomplete_results": incomplete, "items": list(items)})


def issue(number, title, closed_at=None, is_pr=False):
    row = {"number": number, "title": title, "body": "", "closed_at": closed_at}
    if is_pr:
        row["pull_request"] = {"url": "x"}
    return row


def pull_json():
    return json.dumps({"number": PR, "user": {"login": "dependabot[bot]", "type": "Bot"}, "body": "",
                       "head": {"sha": HEAD_SHA}, "state": "open", "merged_at": None})


def every_call(gh):
    """Drive every public GitHub method once through `gh`."""
    source = GhSource(REPO, run=gh)
    source.unlabelled_merged(LABEL, SINCE, 10)
    source.labelled_merged(LABEL, SINCE, 10)
    source.add_label(PR, LABEL)
    source.body(PR)
    issues = GhIssues(REPO, run=gh)
    issues.recent_titles(FEEDBACK, SINCE)
    issues.create("title", "body", [FEEDBACK])
    pulls = GhPullRequests(REPO, run=gh)
    pulls.view(PR)
    pulls.set_body(PR, "body")
    pulls.runs(HEAD_SHA, WORKFLOW, 10)
    pulls.rerun_failed(RUN_ID)
    GhContents(run=gh).read(REPO, "README.md")
    open_feedback_issues(REPO, FEEDBACK, run=gh)


def answering_everything():
    return FakeGh(answers={
        "search/issues": search_page(),
        f"repos/{REPO}/pulls/{PR}": pull_json(),
        ("GET", f"repos/{REPO}/issues"): "[]",
        ("POST", f"repos/{REPO}/issues"): json.dumps({"html_url": ISSUE_URL}),
        f"repos/{REPO}/actions/workflows/{WORKFLOW}/runs": json.dumps({"workflow_runs": []}),
    }, default="{}")


class NoCommandPathUsesGraphQL(unittest.TestCase):
    def test_every_github_call_is_gh_api(self):
        gh = answering_everything()

        every_call(gh)

        self.assertEqual({tuple(argv[:2]) for argv in gh.argvs()}, {("gh", "api")})

    def test_no_github_call_targets_graphql(self):
        gh = answering_everything()

        every_call(gh)

        self.assertFalse([argv for argv in gh.argvs() if path_of(argv).strip("/").startswith("graphql")])

    def test_every_github_call_names_its_method(self):
        gh = answering_everything()

        every_call(gh)

        self.assertTrue(all(method_of(argv) in {"GET", "POST", "PATCH"} for argv in gh.argvs()))

    def test_a_graphql_path_is_refused_before_gh_runs(self):
        gh = FakeGh()

        with self.assertRaises(SourceError):
            GhApi(run=gh).call("POST", "graphql")

        self.assertEqual(gh.calls, [])


class TheWatermarkSearch(unittest.TestCase):
    def test_the_ingest_search_excludes_the_label_and_starts_at_the_watermark_oldest_first(self):
        gh = FakeGh(default=search_page())

        GhSource(REPO, run=gh).unlabelled_merged(LABEL, SINCE, 50)

        q = query_of(gh.argvs()[0])
        self.assertEqual((q["q"], q["sort"], q["order"]),
                         (f"repo:{REPO} is:pr is:merged -label:{LABEL} merged:>={SINCE}", "created", "asc"))

    def test_the_ingest_search_without_a_watermark_has_no_date(self):
        gh = FakeGh(default=search_page())

        GhSource(REPO, run=gh).unlabelled_merged(LABEL, None, 50)

        self.assertNotIn("merged:", query_of(gh.argvs()[0])["q"])

    def test_the_ledger_rebuild_reads_labelled_prs_newest_first(self):
        gh = FakeGh(default=search_page())

        GhSource(REPO, run=gh).labelled_merged(LABEL, SINCE, 500)

        q = query_of(gh.argvs()[0])
        self.assertEqual((q["q"], q["order"]), (f"repo:{REPO} is:pr is:merged label:{LABEL} merged:>={SINCE}", "desc"))

    def test_a_search_result_keeps_number_body_and_merge_time(self):
        gh = FakeGh(default=search_page(search_item(7, "the body", "2026-10-02T08:00:00Z")))

        pulls = GhSource(REPO, run=gh).unlabelled_merged(LABEL, SINCE, 50)

        self.assertEqual([(p.number, p.body, p.merged_at) for p in pulls], [(7, "the body", "2026-10-02T08:00:00Z")])

    def test_a_null_body_reads_as_empty(self):
        gh = FakeGh(default=search_page(search_item(7, None)))

        pulls = GhSource(REPO, run=gh).unlabelled_merged(LABEL, SINCE, 50)

        self.assertEqual(pulls[0].body, "")

    def test_incomplete_search_results_are_refused(self):
        gh = FakeGh(default=search_page(search_item(7), incomplete=True))

        with self.assertRaises(SourceError):
            GhSource(REPO, run=gh).unlabelled_merged(LABEL, SINCE, 50)

    def test_a_limit_beyond_the_search_cap_is_refused_before_gh_runs(self):
        gh = FakeGh(default=search_page())

        with self.assertRaises(SourceError):
            GhSource(REPO, run=gh).labelled_merged(LABEL, SINCE, SEARCH_RESULT_CAP + 1)

        self.assertEqual(gh.calls, [])

    def test_a_search_item_without_a_pull_request_is_refused(self):
        gh = FakeGh(default=search_page({"number": 7, "body": ""}))

        with self.assertRaises(SourceError):
            GhSource(REPO, run=gh).unlabelled_merged(LABEL, SINCE, 50)


class Paging(unittest.TestCase):
    def test_a_full_page_leads_to_the_next(self):
        gh = FakeGh(answers={"search/issues": [search_page(*[search_item(n) for n in range(100)]),
                                                search_page(*[search_item(n) for n in range(100, 150)])]})

        pulls = GhSource(REPO, run=gh).unlabelled_merged(LABEL, SINCE, 500)

        self.assertEqual((len(pulls), [query_of(a)["page"] for a in gh.argvs()]), (150, ["1", "2"]))

    def test_a_short_page_is_the_last_one_read(self):
        gh = FakeGh(answers={"search/issues": [search_page(search_item(1))]}, default=search_page(search_item(2)))

        GhSource(REPO, run=gh).unlabelled_merged(LABEL, SINCE, 500)

        self.assertEqual(len(gh.calls), 1)

    def test_reading_stops_at_the_limit(self):
        full = search_page(*[search_item(n) for n in range(100)])
        gh = FakeGh(default=full)

        pulls = GhSource(REPO, run=gh).unlabelled_merged(LABEL, SINCE, 120)

        self.assertEqual((len(pulls), len(gh.calls)), (120, 2))

    def test_a_small_limit_asks_for_a_small_page(self):
        gh = FakeGh(default=search_page())

        GhSource(REPO, run=gh).unlabelled_merged(LABEL, SINCE, 10)

        self.assertEqual(query_of(gh.argvs()[0])["per_page"], "10")


class IssueListing(unittest.TestCase):
    def test_pull_requests_are_left_out_of_issue_titles(self):
        gh = FakeGh(default=json.dumps([issue(1, "an issue"), issue(2, "a pull request", is_pr=True)]))

        titles = GhIssues(REPO, run=gh).recent_titles(FEEDBACK, None)

        self.assertEqual(titles, ["an issue"])

    def test_a_closed_issue_counts_only_when_it_closed_in_the_window(self):
        gh = FakeGh(answers={f"repos/{REPO}/issues": [
            json.dumps([issue(1, "open")]),
            json.dumps([issue(2, "closed in window", "2026-09-02T00:00:00Z"),
                        issue(3, "closed before", "2026-08-30T00:00:00Z")]),
        ]})

        titles = GhIssues(REPO, run=gh).recent_titles(FEEDBACK, SINCE)

        self.assertEqual(titles, ["open", "closed in window"])

    def test_issue_listings_filter_by_label_and_state(self):
        gh = FakeGh(default="[]")

        GhIssues(REPO, run=gh).recent_titles(FEEDBACK, SINCE)

        self.assertEqual([(query_of(a)["labels"], query_of(a)["state"]) for a in gh.argvs()],
                         [(FEEDBACK, "open"), (FEEDBACK, "closed")])

    def test_open_feedback_issues_leave_out_pull_requests(self):
        gh = FakeGh(default=json.dumps([issue(1, "an issue"), issue(2, "a pull request", is_pr=True)]))

        found = open_feedback_issues(REPO, FEEDBACK, run=gh)

        self.assertEqual([i.number for i in found], [1])

    def test_creating_an_issue_sends_it_on_stdin_and_returns_its_url(self):
        gh = FakeGh(default=json.dumps({"html_url": ISSUE_URL}))

        url = GhIssues(REPO, run=gh).create("the title", "the body", [FEEDBACK])

        (argv, stdin), = gh.calls
        self.assertEqual((method_of(argv), path_of(argv), json.loads(stdin), url),
                         ("POST", f"repos/{REPO}/issues",
                          {"title": "the title", "body": "the body", "labels": [FEEDBACK]}, ISSUE_URL))

    def test_an_issue_listing_that_is_not_a_list_is_refused(self):
        gh = FakeGh(default=json.dumps({"message": "x"}))

        with self.assertRaises(SourceError):
            GhIssues(REPO, run=gh).recent_titles(FEEDBACK, None)


class Labels(unittest.TestCase):
    def test_labelling_a_pr_adds_the_label_to_its_issue(self):
        gh = FakeGh(default="[]")

        GhSource(REPO, run=gh).add_label(PR, LABEL)

        (argv, stdin), = gh.calls
        self.assertEqual((method_of(argv), path_of(argv), json.loads(stdin)),
                         ("POST", f"repos/{REPO}/issues/{PR}/labels", {"labels": [LABEL]}))


class Contents(unittest.TestCase):
    def test_a_missing_file_reads_as_none(self):
        gh = FakeGh(returncode=1, stderr="gh: Not Found (HTTP 404)")

        self.assertIsNone(GhContents(run=gh).read(REPO, "README.md"))

    def test_another_failure_is_an_error(self):
        gh = FakeGh(returncode=1, stderr="gh: Bad credentials (HTTP 401)")

        with self.assertRaises(SourceError):
            GhContents(run=gh).read(REPO, "README.md")

    def test_a_file_is_read_raw(self):
        gh = FakeGh(default="text")

        GhContents(run=gh).read(REPO, "README.md")

        self.assertIn("Accept: application/vnd.github.raw", gh.argvs()[0])


class Failures(unittest.TestCase):
    def test_a_failing_gh_is_an_error_naming_the_call(self):
        gh = FakeGh(returncode=1, stderr="gh: Server Error (HTTP 500)")

        with self.assertRaises(SourceError) as caught:
            GhSource(REPO, run=gh).body(PR)

        self.assertIn(f"GET repos/{REPO}/pulls/{PR}", str(caught.exception))

    def test_output_that_is_not_json_is_refused(self):
        gh = FakeGh(default="<html>")

        with self.assertRaises(SourceError):
            GhSource(REPO, run=gh).body(PR)


if __name__ == "__main__":
    unittest.main()
