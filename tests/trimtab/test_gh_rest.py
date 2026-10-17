"""GitHub through `gh api` on repository-scoped REST paths only.

Claude Code cloud sessions refuse GraphQL, and gh's porcelain uses it; they
also refuse every REST path outside `repos/{owner}/{repo}/...`, `search/`
included.
"""

import json
import unittest
from pathlib import Path

from gh_fake import FakeGh, method_of, path_of, query_of
from trimtab.capture.sources import (
    ISSUE_LIMIT, SCAN_LIMIT, GhApi, GhBodies, GhContents, GhIssues, GhPullRequests, GhSource, NotFound, Refused,
    SourceError,
    open_feedback_issues, staged_files,
)

REPO = "owner/repo"
LABEL = "trimtab-ingested"
OTHER_LABEL = "dependencies"
FEEDBACK = "harness-feedback"
SINCE = "2026-09-01"
WINDOW_START = "2026-08-01"
BEFORE_SINCE = "2026-08-20T00:00:00Z"
AFTER_SINCE = "2026-09-10T00:00:00Z"
PR = 76
RUN_ID = 1001
HEAD_SHA = "a" * 40
WORKFLOW = "ci.yml"
ISSUE_URL = "https://github.com/owner/repo/issues/9"
PULLS = f"repos/{REPO}/pulls"


def pull(number, merged_at=AFTER_SINCE, updated_at=None, labels=(), body="", title=""):
    """One row of `GET repos/{o}/{r}/pulls`: a merged PR unless merged_at is None."""
    return {"number": number, "title": title, "body": body, "merged_at": merged_at,
            "updated_at": updated_at or merged_at or AFTER_SINCE, "labels": [{"name": n} for n in labels]}


def page(*rows):
    return json.dumps(list(rows))


def full_page(start, **kwargs):
    return page(*[pull(n, **kwargs) for n in range(start, start + 100)])


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
    source.unlabelled_merged(LABEL, None, 10)
    source.labelled_merged(LABEL, SINCE, 10)
    source.recent_pulls(SINCE)
    source.add_label(PR, LABEL)
    source.body(PR)
    issues = GhIssues(REPO, run=gh, settle_seconds=0)
    issues.recent_titles(FEEDBACK, SINCE)
    with issues.filing() as filer:
        filer.create("title", "body", [FEEDBACK])
    bodies = GhBodies(REPO, run=gh)
    for kind in ("pull", "issue", "comment"):
        bodies.read(kind, PR)
        bodies.write(kind, PR, "body")
    pulls = GhPullRequests(REPO, run=gh)
    pulls.view(PR)
    pulls.set_body(PR, "body")
    pulls.runs(HEAD_SHA, WORKFLOW, 10)
    pulls.rerun_failed(RUN_ID)
    GhContents(run=gh).read(REPO, "README.md")
    open_feedback_issues(REPO, FEEDBACK, run=gh)


def answering_everything():
    return FakeGh(answers={
        ("GET", PULLS): "[]",
        f"repos/{REPO}/pulls/{PR}": pull_json(),
        ("GET", f"repos/{REPO}/issues"): "[]",
        ("POST", f"repos/{REPO}/issues"): json.dumps({"html_url": ISSUE_URL, "number": 9}),
        ("GET", f"repos/{REPO}/issues/9"): json.dumps({"body": "body"}),
        f"repos/{REPO}/actions/workflows/{WORKFLOW}/runs": json.dumps({"workflow_runs": []}),
    }, default="{}")


class EveryCallIsRepositoryScopedRest(unittest.TestCase):
    def test_every_github_call_is_gh_api(self):
        gh = answering_everything()

        every_call(gh)

        self.assertEqual({tuple(argv[:2]) for argv in gh.argvs()}, {("gh", "api")})

    def test_every_github_call_targets_a_repository_path(self):
        gh = answering_everything()

        every_call(gh)

        self.assertEqual([path_of(a) for a in gh.argvs() if not path_of(a).startswith(f"repos/{REPO}/")], [])

    def test_every_github_call_names_its_method(self):
        gh = answering_everything()

        every_call(gh)

        self.assertTrue(all(method_of(argv) in {"GET", "POST", "PATCH"} for argv in gh.argvs()))

    def test_a_graphql_path_is_refused_before_gh_runs(self):
        gh = FakeGh()

        with self.assertRaises(SourceError):
            GhApi(run=gh).call("POST", "graphql")

        self.assertEqual(gh.calls, [])

    def test_a_path_outside_repos_is_refused_before_gh_runs(self):
        for path in ("search/issues", "/search/issues", "user", "orgs/owner/repos", "notifications",
                     "repositories", "reposx/owner/repo"):
            with self.subTest(path=path):
                gh = FakeGh()

                with self.assertRaises(SourceError):
                    GhApi(run=gh).call("GET", path)

                self.assertEqual(gh.calls, [])

    def test_a_path_with_a_dot_segment_is_refused_before_gh_runs(self):
        # The server may normalise a dot segment into a path outside repos/.
        for path in ("repos/../search/issues", "repos/o/r/../../search/issues", "repos/./o/r",
                     "/repos/../user"):
            with self.subTest(path=path):
                gh = FakeGh()

                with self.assertRaises(SourceError):
                    GhApi(run=gh).call("GET", path)

                self.assertEqual(gh.calls, [])

    def test_a_percent_encoded_dot_segment_is_refused_before_gh_runs(self):
        for path in ("repos/%2e%2e/search/issues", "repos/o/r/%2E%2E/%2e%2e/user", "repos/o/r/.%2e/x"):
            with self.subTest(path=path):
                gh = FakeGh()

                with self.assertRaises(SourceError):
                    GhApi(run=gh).call("GET", path)

                self.assertEqual(gh.calls, [])

    def test_a_percent_encoded_workflow_name_still_passes(self):
        gh = FakeGh(default=json.dumps({"workflow_runs": []}))

        GhPullRequests(REPO, run=gh).runs(HEAD_SHA, "ci checks.yml", 10)

        self.assertEqual(path_of(gh.argvs()[0]), f"repos/{REPO}/actions/workflows/ci%20checks.yml/runs")


class RecentPulls(unittest.TestCase):
    """What a project draft is checked against: open PRs, and closed ones updated within the window."""

    def test_open_merged_and_closed_prs_are_told_apart_by_title(self):
        gh = FakeGh(answers={("GET", PULLS): [page({"number": 3, "title": "[X-1] open"}),
                                              page(pull(1, title="[X-1] merged"),
                                                   pull(2, merged_at=None, title="[X-1] declined"))]})

        found = GhSource(REPO, run=gh).recent_pulls(SINCE)

        self.assertEqual([(p.number, p.title, p.state) for p in found],
                         [(3, "[X-1] open", "open"), (1, "[X-1] merged", "merged"), (2, "[X-1] declined", "closed")])

    def test_a_closed_pr_last_updated_before_the_window_is_left_out(self):
        gh = FakeGh(answers={("GET", PULLS): [page(), page(pull(1, merged_at=BEFORE_SINCE, title="[X-1] old"))]})

        self.assertEqual(GhSource(REPO, run=gh).recent_pulls(SINCE), [])

    def test_a_closed_pr_with_no_title_is_refused_not_silently_unmatched(self):
        for broken in ({k: v for k, v in pull(1).items() if k != "title"}, {**pull(1), "title": None}):
            with self.subTest(title="title" in broken):
                gh = FakeGh(answers={("GET", PULLS): [page(), page(broken)]})

                with self.assertRaises(SourceError):
                    GhSource(REPO, run=gh).recent_pulls(SINCE)

    def test_an_earlier_date_after_a_later_one_continues_the_scan_instead_of_starting_again(self):
        # Page 1 reaches the later date; page 2 reaches the earlier one.
        first = page(*[pull(n) for n in range(0, 99)], pull(99, AFTER_SINCE, updated_at="2026-09-05T00:00:00Z"))
        second = page(pull(100, BEFORE_SINCE))
        gh = FakeGh(answers={("GET", PULLS): [first, second]})
        source = GhSource(REPO, run=gh)

        source.unlabelled_merged(LABEL, "2026-09-08", 50)
        source.labelled_merged(LABEL, SINCE, 50)

        self.assertEqual([query_of(a)["page"] for a in gh.argvs()], ["1", "2"])

    def test_a_closed_prs_close_time_is_carried(self):
        gh = FakeGh(answers={("GET", PULLS): [page(), page({**pull(2, merged_at=None, title="[X-1] declined"),
                                                            "closed_at": AFTER_SINCE})]})

        found = GhSource(REPO, run=gh).recent_pulls(SINCE)

        self.assertEqual([(p.number, p.closed_at) for p in found], [(2, AFTER_SINCE)])

    def test_with_no_window_only_open_prs_are_read(self):
        gh = FakeGh(answers={("GET", PULLS): [page({"number": 3, "title": "[X-1] open"})]})

        found = GhSource(REPO, run=gh).recent_pulls(None)

        self.assertEqual(([p.number for p in found], len(gh.calls)), ([3], 1))


class TheClosedPullsScan(unittest.TestCase):
    """Merged PRs come from the repository's closed pulls, most recently updated first, filtered here."""

    def test_merged_prs_are_read_from_closed_pulls_most_recently_updated_first(self):
        gh = FakeGh(default=page())

        GhSource(REPO, run=gh).unlabelled_merged(LABEL, SINCE, 50)

        argv = gh.argvs()[0]
        q = query_of(argv)
        self.assertEqual((method_of(argv), path_of(argv), q["state"], q["sort"], q["direction"], q["per_page"]),
                         ("GET", PULLS, "closed", "updated", "desc", "100"))

    def test_reading_stops_at_the_first_page_that_reaches_the_floor(self):
        last_page = page(*[pull(n) for n in range(100, 199)], pull(199, BEFORE_SINCE))
        gh = FakeGh(answers={PULLS: [full_page(0), last_page]}, default=full_page(200))

        GhSource(REPO, run=gh).unlabelled_merged(LABEL, SINCE, 50)

        self.assertEqual([query_of(a)["page"] for a in gh.argvs()], ["1", "2"])

    def test_a_short_page_is_the_last_one_read(self):
        gh = FakeGh(answers={PULLS: [page(pull(1))]}, default=full_page(200))

        GhSource(REPO, run=gh).unlabelled_merged(LABEL, SINCE, 50)

        self.assertEqual(len(gh.calls), 1)

    def test_a_closed_pr_that_was_not_merged_is_left_out(self):
        gh = FakeGh(default=page(pull(1), pull(2, merged_at=None)))

        pulls = GhSource(REPO, run=gh).unlabelled_merged(LABEL, SINCE, 50)

        self.assertEqual([p.number for p in pulls], [1])

    def test_a_pr_merged_before_the_floor_and_updated_after_it_is_left_out(self):
        gh = FakeGh(default=page(pull(1), pull(2, merged_at=BEFORE_SINCE, updated_at=AFTER_SINCE)))

        pulls = GhSource(REPO, run=gh).unlabelled_merged(LABEL, SINCE, 50)

        self.assertEqual([p.number for p in pulls], [1])

    def test_ingest_leaves_out_prs_already_labelled(self):
        gh = FakeGh(default=page(pull(1, labels=[LABEL]), pull(2, labels=[OTHER_LABEL])))

        pulls = GhSource(REPO, run=gh).unlabelled_merged(LABEL, SINCE, 50)

        self.assertEqual([p.number for p in pulls], [2])

    def test_the_ledger_rebuild_keeps_only_labelled_prs(self):
        gh = FakeGh(default=page(pull(1, labels=[LABEL]), pull(2, labels=[OTHER_LABEL])))

        pulls = GhSource(REPO, run=gh).labelled_merged(LABEL, SINCE, 50)

        self.assertEqual([p.number for p in pulls], [1])

    def test_ingest_takes_the_oldest_merged_first_up_to_its_limit(self):
        gh = FakeGh(default=page(pull(3, "2026-09-12T00:00:00Z"), pull(1, "2026-09-14T00:00:00Z"),
                                 pull(2, "2026-09-11T00:00:00Z")))

        pulls = GhSource(REPO, run=gh).unlabelled_merged(LABEL, SINCE, 2)

        self.assertEqual([p.number for p in pulls], [2, 3])

    def test_the_ledger_rebuild_takes_the_newest_merged_first_up_to_its_limit(self):
        gh = FakeGh(default=page(pull(3, "2026-09-12T00:00:00Z", labels=[LABEL]),
                                 pull(1, "2026-09-14T00:00:00Z", labels=[LABEL]),
                                 pull(2, "2026-09-11T00:00:00Z", labels=[LABEL])))

        pulls = GhSource(REPO, run=gh).labelled_merged(LABEL, SINCE, 2)

        self.assertEqual([p.number for p in pulls], [1, 3])

    def test_a_pr_seen_on_two_pages_counts_once(self):
        # A PR updated during the scan moves up the order and shifts the pages.
        first = full_page(0)
        second = page(pull(99), pull(100, BEFORE_SINCE))
        gh = FakeGh(answers={PULLS: [first, second]})

        pulls = GhSource(REPO, run=gh).unlabelled_merged(LABEL, SINCE, 500)

        self.assertEqual(len(pulls), 100)

    def test_a_pr_keeps_number_body_and_merge_time(self):
        gh = FakeGh(default=page(pull(7, "2026-10-02T08:00:00Z", body="the body")))

        pulls = GhSource(REPO, run=gh).unlabelled_merged(LABEL, SINCE, 50)

        self.assertEqual([(p.number, p.body, p.merged_at) for p in pulls], [(7, "the body", "2026-10-02T08:00:00Z")])

    def test_a_null_body_reads_as_empty(self):
        gh = FakeGh(default=page(pull(7, body=None)))

        pulls = GhSource(REPO, run=gh).unlabelled_merged(LABEL, SINCE, 50)

        self.assertEqual(pulls[0].body, "")

    def test_more_closed_prs_than_the_scan_bound_are_refused_not_cut_off(self):
        # A cut-off scan would let an unlabelled PR slip past the watermark unseen.
        gh = FakeGh(default=full_page(0))

        with self.assertRaises(SourceError):
            GhSource(REPO, run=gh).unlabelled_merged(LABEL, SINCE, 50)

        self.assertEqual(len(gh.calls), SCAN_LIMIT // 100)

    def test_a_limit_beyond_the_scan_bound_is_refused_before_gh_runs(self):
        gh = FakeGh(default=page())

        with self.assertRaises(SourceError):
            GhSource(REPO, run=gh).labelled_merged(LABEL, SINCE, SCAN_LIMIT + 1)

        self.assertEqual(gh.calls, [])

    def test_a_row_missing_its_merge_fields_is_refused(self):
        gh = FakeGh(default=page({"number": 7, "body": ""}))

        with self.assertRaises(SourceError):
            GhSource(REPO, run=gh).unlabelled_merged(LABEL, SINCE, 50)

    def test_a_listing_that_is_not_a_list_is_refused(self):
        gh = FakeGh(default=json.dumps({"message": "x"}))

        with self.assertRaises(SourceError):
            GhSource(REPO, run=gh).unlabelled_merged(LABEL, SINCE, 50)

    def test_the_ledger_rebuild_and_ingest_in_one_run_read_the_listing_once(self):
        gh = FakeGh(answers={PULLS: [page(pull(1, labels=[LABEL]), pull(2))]})
        source = GhSource(REPO, run=gh)

        rebuilt = source.labelled_merged(LABEL, WINDOW_START, 500)
        todo = source.unlabelled_merged(LABEL, SINCE, 50)

        self.assertEqual(([p.number for p in rebuilt], [p.number for p in todo], len(gh.calls)), ([1], [2], 1))


class WithoutAFloor(unittest.TestCase):
    """A backfill has no date: closed pulls are read oldest created first, until the limit is met."""

    def test_closed_pulls_are_read_oldest_created_first(self):
        gh = FakeGh(default=page())

        GhSource(REPO, run=gh).unlabelled_merged(LABEL, None, 50)

        q = query_of(gh.argvs()[0])
        self.assertEqual((q["state"], q["sort"], q["direction"]), ("closed", "created", "asc"))

    def test_reading_stops_once_the_limit_is_met(self):
        gh = FakeGh(answers={PULLS: [full_page(0, labels=[LABEL]), full_page(100)]}, default=full_page(200))

        pulls = GhSource(REPO, run=gh).unlabelled_merged(LABEL, None, 50)

        self.assertEqual(([p.number for p in pulls][:2], len(pulls), len(gh.calls)), ([100, 101], 50, 2))

    def test_a_scan_past_its_bound_is_refused_not_cut_off(self):
        gh = FakeGh(default=full_page(0, labels=[LABEL]))

        with self.assertRaises(SourceError):
            GhSource(REPO, run=gh).unlabelled_merged(LABEL, None, 50)

        self.assertEqual(len(gh.calls), SCAN_LIMIT // 100)

    def test_a_backfill_past_its_bound_names_a_dated_read(self):
        # Lowering the limit does not help when labelled PRs fill the bound.
        gh = FakeGh(default=full_page(0, labels=[LABEL]))

        with self.assertRaises(SourceError) as caught:
            GhSource(REPO, run=gh).unlabelled_merged(LABEL, None, 50)

        self.assertIn("--backfill", str(caught.exception))


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
        gh = FakeGh(answers={("POST", f"repos/{REPO}/issues"): json.dumps({"html_url": ISSUE_URL, "number": 9})},
                    default=json.dumps({"body": "the body"}))

        with GhIssues(REPO, run=gh, settle_seconds=0).filing() as filer:
            url = filer.create("the title", "the body", [FEEDBACK])

        argv, stdin = gh.calls[0]
        self.assertEqual((method_of(argv), path_of(argv), json.loads(stdin), url),
                         ("POST", f"repos/{REPO}/issues",
                          {"title": "the title", "body": "the body", "labels": [FEEDBACK]}, ISSUE_URL))

    def test_an_issue_listing_past_its_limit_is_refused_not_cut_off(self):
        # A cut-off list could hide the issue that makes a proposal a duplicate.
        gh = FakeGh(default=json.dumps([issue(n, f"t{n}") for n in range(100)]))

        with self.assertRaises(SourceError):
            GhIssues(REPO, run=gh).recent_titles(FEEDBACK, None)

    def test_an_issue_listing_at_its_limit_is_read_whole(self):
        full = [json.dumps([issue(p * 100 + n, f"t{p}-{n}") for n in range(100)]) for p in range(ISSUE_LIMIT // 100)]
        gh = FakeGh(answers={f"repos/{REPO}/issues": full + ["[]"]})

        titles = GhIssues(REPO, run=gh).recent_titles(FEEDBACK, None)

        self.assertEqual(len(titles), ISSUE_LIMIT)

    def test_reading_an_issue_listing_stops_at_a_short_page(self):
        gh = FakeGh(answers={f"repos/{REPO}/issues": [json.dumps([issue(1, "t1")])]},
                    default=json.dumps([issue(n, f"t{n}") for n in range(100)]))

        GhIssues(REPO, run=gh).recent_titles(FEEDBACK, None)

        self.assertEqual(len(gh.calls), 1)

    def test_open_feedback_issues_past_their_limit_are_refused(self):
        gh = FakeGh(default=json.dumps([issue(n, f"t{n}") for n in range(100)]))

        with self.assertRaises(SourceError):
            open_feedback_issues(REPO, FEEDBACK, run=gh)

    def test_a_closed_since_that_is_not_a_day_is_refused_before_gh_runs(self):
        gh = FakeGh(default="[]")

        with self.assertRaises(SourceError):
            GhIssues(REPO, run=gh).recent_titles(FEEDBACK, "2026-09-01T00:00:00Z")

        self.assertEqual(gh.calls, [])

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

    def test_gh_error_text_is_read_for_its_status(self):
        # gh gives the status only in its error text (ADR 0024): these are the forms read.
        cases = {"gh: Not Found (HTTP 404)": NotFound, "gh: Validation Failed (HTTP 422)": Refused,
                 "gh: API rate limit exceeded (HTTP 429)": Refused}
        for stderr, kind in cases.items():
            with self.subTest(stderr=stderr):
                with self.assertRaises(kind):
                    GhSource(REPO, run=FakeGh(returncode=1, stderr=stderr)).body(PR)

    def test_a_server_error_or_an_unreadable_status_is_not_a_refusal(self):
        # Not a refusal means "may have filed": a misread errs towards stopping, never towards a re-run.
        for stderr in ("gh: Bad Gateway (HTTP 502)", "gh: something failed", "error: status 422"):
            with self.subTest(stderr=stderr):
                with self.assertRaises(SourceError) as caught:
                    GhSource(REPO, run=FakeGh(returncode=1, stderr=stderr)).body(PR)

                self.assertNotIsInstance(caught.exception, Refused)

    def test_output_that_is_not_json_is_refused(self):
        gh = FakeGh(default="<html>")

        with self.assertRaises(SourceError):
            GhSource(REPO, run=gh).body(PR)


class StagedFiles(unittest.TestCase):
    def test_a_git_failure_never_prints_an_id_shape_from_its_message(self):
        # git names the path it could not use; a path can hold a run's ID, in any case.
        for tail in ("01" + "A1b2C3d4E5f6G7h8J9k0", "abcdefghij" + "0123456789"):
            with self.subTest(tail=tail):
                missing = Path("/nonexistent") / ("notes-" + "session_" + tail)

                with self.assertRaises(SourceError) as caught:
                    staged_files(missing)

                self.assertNotIn(tail, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
