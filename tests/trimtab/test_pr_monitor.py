"""The own-PR list's pure core (ADR 0028): what counts as a create, the canonical key, the
list's format and bounds, and the verdict for one set_monitor call.

Pure: no filesystem, no network, a fixed clock. The hook edges are tested through the
guard matrix.
"""

import json
import unittest

from trimtab.adapters import pr_monitor as pm
from trimtab.adapters.pr_monitor import Entry, PrKey, Verdict

NOW = 1_800_000_000
DAY = 24 * 60 * 60
OWN_URL = "https://github.com/owner/repo/pull/7"
OWN_KEY = PrKey("github.com", "owner", "repo", 7)
OTHER_NUMBER_URL = "https://github.com/owner/repo/pull/8"
OTHER_REPO_URL = "https://github.com/owner/elsewhere/pull/7"
OTHER_HOST_URL = "https://ghe.example.com/owner/repo/pull/7"
GHE_URL = "https://ghe.example.com/team/service/pull/42"
SESSION = "session-under-test"


def bash_result(command, stdout, **response):
    """A PostToolUse payload for a Bash call that returned `stdout`."""
    return {"hook_event_name": "PostToolUse", "tool_name": "Bash",
            "tool_input": {"command": command},
            "tool_response": {"stdout": stdout, "stderr": "", "interrupted": False, **response}}


def api_response(url=OWN_URL, number=7, full_name="owner/repo"):
    data = {"html_url": url, "number": number, "base": {"repo": {"full_name": full_name}}}
    return json.dumps(data)


def recorded(*keys, at=NOW):
    return tuple(Entry(k, at) for k in keys)


class Canonical(unittest.TestCase):
    def test_a_github_pr_url_reduces_to_its_key(self):
        self.assertEqual(pm.canonical(OWN_URL), OWN_KEY)

    def test_a_trailing_slash_is_the_same_pr(self):
        self.assertEqual(pm.canonical(OWN_URL + "/"), OWN_KEY)

    def test_owner_and_repository_case_is_ignored(self):
        self.assertEqual(pm.canonical("https://github.com/Owner/REPO/pull/7"), OWN_KEY)

    def test_host_case_is_ignored(self):
        self.assertEqual(pm.canonical("https://GitHub.com/owner/repo/pull/7"), OWN_KEY)

    def test_a_query_and_fragment_are_dropped(self):
        self.assertEqual(pm.canonical(OWN_URL + "?x=1#y"), OWN_KEY)

    def test_an_enterprise_host_is_kept_in_the_key(self):
        self.assertEqual(pm.canonical(GHE_URL), PrKey("ghe.example.com", "team", "service", 42))

    def test_surrounding_whitespace_is_ignored(self):
        self.assertEqual(pm.canonical(f"  {OWN_URL}\n"), OWN_KEY)

    def test_shapes_that_are_not_a_pr_page_have_no_key(self):
        for url in ("http://github.com/owner/repo/pull/7",
                    "https://user@github.com/owner/repo/pull/7",
                    "https://github.com:443/owner/repo/pull/7",
                    "https://github.com/owner/repo/pulls/7",
                    "https://github.com/owner/repo/pull/7/files",
                    "https://api.github.com/repos/owner/repo/pulls/7",
                    "https://github.com/owner/repo/pull/07",
                    "https://github.com/owner/repo/pull/0",
                    "https://github.com/owner/repo/pull/7%2F",
                    "https://github.com/owner/../pull/7",
                    "https://github.com/-owner/repo/pull/7",
                    "https://github/owner/repo/pull/7",
                    "https://github.com/owner/repo/pull/7 extra",
                    "https://github.com/owner/rëpo/pull/7",
                    "", None, 7):
            with self.subTest(url=url):
                self.assertIsNone(pm.canonical(url))


class CreatedPr(unittest.TestCase):
    def test_a_lone_gh_pr_create_records_its_url(self):
        self.assertEqual(pm.created_pr(bash_result("gh pr create --draft --title t --body-file b.md",
                                                   OWN_URL + "\n")), OWN_KEY)

    def test_a_create_after_cd_records_its_url(self):
        self.assertEqual(pm.created_pr(bash_result("cd /work/repo && gh pr create --fill", OWN_URL)), OWN_KEY)

    def test_a_create_after_git_push_records_its_url(self):
        command = "git push -u origin feature && gh pr create --draft --title t --body-file b.md"
        self.assertEqual(pm.created_pr(bash_result(command, OWN_URL)), OWN_KEY)

    def test_a_create_after_cd_and_git_push_records_its_url(self):
        command = "cd /work/repo && git push -u origin feature && gh pr create --fill"
        self.assertEqual(pm.created_pr(bash_result(command, OWN_URL)), OWN_KEY)

    def test_a_quoted_title_with_spaces_is_still_a_create(self):
        command = "gh pr create --title 'fix: the thing' --body-file b.md"
        self.assertEqual(pm.created_pr(bash_result(command, OWN_URL)), OWN_KEY)

    def test_a_rest_create_records_its_html_url(self):
        command = "gh api -X POST repos/owner/repo/pulls --input body.json"
        self.assertEqual(pm.created_pr(bash_result(command, api_response())), OWN_KEY)

    def test_a_rest_create_with_fields_is_an_implied_post(self):
        command = "gh api repos/{owner}/{repo}/pulls -f title=t -f head=b -f base=main -F body=@b.md"
        self.assertEqual(pm.created_pr(bash_result(command, api_response())), OWN_KEY)

    def test_an_enterprise_rest_create_records_its_host(self):
        command = "gh api --hostname ghe.example.com --method=POST repos/team/service/pulls --input b.json"
        stdout = api_response(GHE_URL, 42, "team/service")
        self.assertEqual(pm.created_pr(bash_result(command, stdout)), PrKey("ghe.example.com", "team", "service", 42))

    def test_a_create_naming_its_repository_records_a_pr_there(self):
        for flag in ("-R owner/repo", "--repo owner/repo", "--repo=owner/repo", "-Rowner/repo",
                     "-R github.com/owner/repo", "--repo https://github.com/owner/repo"):
            with self.subTest(flag=flag):
                self.assertEqual(pm.created_pr(bash_result(f"gh pr create --fill {flag}", OWN_URL)), OWN_KEY)

    def test_an_enterprise_create_naming_its_host_records_its_pr(self):
        command = "gh pr create --fill -R ghe.example.com/team/service"
        self.assertEqual(pm.created_pr(bash_result(command, GHE_URL)), PrKey("ghe.example.com", "team", "service", 42))

    def test_a_pr_on_a_host_the_create_did_not_name_is_not_recorded(self):
        for command, stdout in (("gh pr create --fill", GHE_URL),
                                ("gh pr create --fill -R ghe.example.com/owner/repo", OWN_URL),
                                ("gh pr create --fill -R owner/repo", OTHER_HOST_URL)):
            with self.subTest(command=command):
                self.assertIsNone(pm.created_pr(bash_result(command, stdout)))

    def test_a_pr_in_a_repository_the_create_did_not_name_is_not_recorded(self):
        self.assertIsNone(pm.created_pr(bash_result("gh pr create --fill -R owner/elsewhere", OWN_URL)))

    def test_an_unclear_repository_flag_records_nothing(self):
        for flag in ("-R owner/repo -R owner/repo", "-R owner", "-R a/b/c/d", "-R", "--repo http://github.com/owner/repo",
                     "-R owner/..", "-R bad_host/owner/repo"):
            with self.subTest(flag=flag):
                self.assertIsNone(pm.created_pr(bash_result(f"gh pr create --fill {flag}", OWN_URL)))

    def test_a_rest_response_from_another_host_is_not_recorded(self):
        for command in ("gh api --hostname evil.example.com -X POST repos/owner/repo/pulls --input b.json",
                        "gh api --hostname=evil.example.com -X POST repos/owner/repo/pulls --input b.json"):
            with self.subTest(command=command):
                self.assertIsNone(pm.created_pr(bash_result(command, api_response())))

    def test_a_rest_create_without_a_hostname_records_only_github_com(self):
        command = "gh api -X POST repos/team/service/pulls --input b.json"
        self.assertIsNone(pm.created_pr(bash_result(command, api_response(GHE_URL, 42, "team/service"))))

    def test_a_rest_response_for_another_repository_than_the_path_is_not_recorded(self):
        command = "gh api -X POST repos/owner/repo/pulls --input b.json"
        stdout = api_response(OTHER_REPO_URL, 7, "owner/other")
        self.assertIsNone(pm.created_pr(bash_result(command, stdout)))

    def test_commands_that_could_print_another_url_record_nothing(self):
        for command in ("echo " + OWN_URL,
                        "gh pr create --fill; echo x",
                        "gh pr create --fill || echo x",
                        "gh pr create --fill | tee out",
                        "gh pr create --fill > out",
                        "gh pr create --fill &",
                        "gh pr create --fill && echo x",
                        'gh pr create --body "$(cat b.md)"',
                        "gh pr create --body `cat b.md`",
                        "gh pr create --title $TITLE",
                        "gh pr create --title a\\;b",
                        "gh pr create --fill\necho x",
                        "/usr/bin/gh pr create --fill",
                        "GH_HOST=x gh pr create --fill",
                        "gh pr create --web",
                        "gh pr create -w",
                        "gh pr create --dry-run",
                        "gh pr view 7",
                        "cd - && gh pr create --fill",
                        "cd && gh pr create --fill",
                        "cd /work/* && gh pr create --fill",
                        "cd /a /b && gh pr create --fill",
                        "git push --porcelain && gh pr create --fill",
                        "git -c core.hooksPath=x push && gh pr create --fill",
                        "git commit -m x && gh pr create --fill",
                        "git push && cd /work && gh pr create --fill",
                        "cd /a && cd /b && gh pr create --fill",
                        "&& gh pr create --fill",
                        "gh pr create --title 'unbalanced"):
            with self.subTest(command=command):
                self.assertIsNone(pm.created_pr(bash_result(command, OWN_URL)))

    def test_rest_calls_that_are_not_a_plain_create_record_nothing(self):
        for command in ("gh api repos/owner/repo/pulls",
                        "gh api -X GET repos/owner/repo/pulls -f state=open",
                        "gh api -X PATCH repos/owner/repo/pulls/7 -f title=t",
                        "gh api -X POST repos/owner/repo/issues -f title=t",
                        "gh api -X POST repos/owner/repo/pulls --input b.json --jq .html_url",
                        "gh api -X POST repos/owner/repo/pulls --input b.json -q .html_url",
                        "gh api -X POST repos/owner/repo/pulls --input b.json --include",
                        "gh api -X POST repos/owner/repo/pulls --input b.json --paginate",
                        "gh api -X POST repos/owner/repo/pulls --input b.json --silent",
                        "gh api -X POST repos/owner/repo/pulls --input b.json --unknown-flag",
                        "gh api -X POST repos/owner/repo/pulls extra --input b.json"):
            with self.subTest(command=command):
                self.assertIsNone(pm.created_pr(bash_result(command, api_response())))

    def test_stdout_that_is_not_exactly_one_pr_url_records_nothing(self):
        for stdout in ("", "\n", f"{OWN_URL}\n{OTHER_NUMBER_URL}\n", f"/work/repo\n{OWN_URL}\n",
                       "https://github.com/owner/repo/compare/main...feature", "Created pull request"):
            with self.subTest(stdout=stdout):
                self.assertIsNone(pm.created_pr(bash_result("gh pr create --fill", stdout)))

    def test_a_url_only_on_stderr_records_nothing(self):
        payload = bash_result("gh pr create --fill", "")
        payload["tool_response"]["stderr"] = OWN_URL

        self.assertIsNone(pm.created_pr(payload))

    def test_an_interrupted_or_partial_call_records_nothing(self):
        for extra in ({"interrupted": True}, {"backgroundTaskId": "task"}, {"persistedOutputPath": "/x/out"}):
            with self.subTest(extra=extra):
                self.assertIsNone(pm.created_pr(bash_result("gh pr create --fill", OWN_URL, **extra)))

    def test_a_rest_response_whose_fields_disagree_records_nothing(self):
        for stdout in (api_response(number=8), api_response(number="7"), api_response(number=True),
                       api_response(full_name="owner/elsewhere"), api_response(full_name=7),
                       api_response(url="https://github.com/owner/repo/pull/7/files"),
                       "[" + api_response() + "]", "not json", f"{OWN_URL}"):
            with self.subTest(stdout=stdout):
                command = "gh api -X POST repos/owner/repo/pulls --input b.json"
                self.assertIsNone(pm.created_pr(bash_result(command, stdout)))

    def test_payloads_that_are_not_a_bash_result_record_nothing(self):
        for payload in (None, [], "text", {}, {"tool_name": "Write"},
                        {"tool_name": "Bash", "tool_input": {"command": "gh pr create"}},
                        {"tool_name": "Bash", "tool_input": "gh pr create", "tool_response": {"stdout": OWN_URL}},
                        {"tool_name": "Bash", "tool_input": {"command": 7}, "tool_response": {"stdout": OWN_URL}},
                        {"tool_name": "Bash", "tool_input": {"command": "gh pr create"}, "tool_response": OWN_URL}):
            with self.subTest(payload=payload):
                self.assertIsNone(pm.created_pr(payload))


class ListName(unittest.TestCase):
    def test_the_file_name_is_a_hash_that_does_not_contain_the_id(self):
        name = pm.list_name(SESSION)

        self.assertRegex(name, r"^[0-9a-f]{64}\.json$")
        self.assertNotIn(SESSION, name)

    def test_the_same_session_always_gets_the_same_file(self):
        self.assertEqual(pm.list_name(SESSION), pm.list_name(SESSION))

    def test_a_malformed_session_id_is_refused(self):
        for session_id in (None, "", "../escape", "a b", "x" * 129, 7, "sess/ion"):
            with self.subTest(session_id=session_id):
                with self.assertRaises(pm.SessionIdError):
                    pm.list_name(session_id)


class ListBounds(unittest.TestCase):
    def test_recording_a_pr_adds_it(self):
        self.assertEqual(pm.add((), OWN_KEY, NOW), recorded(OWN_KEY))

    def test_recording_the_same_pr_twice_leaves_one_entry(self):
        once = pm.add((), OWN_KEY, NOW)

        self.assertEqual(len(pm.add(once, OWN_KEY, NOW)), 1)

    def test_an_entry_older_than_two_days_is_dropped_on_the_next_record(self):
        old = recorded(PrKey("github.com", "owner", "repo", 1), at=NOW - 2 * DAY)

        self.assertEqual(pm.add(old, OWN_KEY, NOW), recorded(OWN_KEY))

    def test_the_list_keeps_only_the_newest_thirty_prs(self):
        entries = ()
        for n in range(1, 36):
            entries = pm.add(entries, PrKey("github.com", "owner", "repo", n), NOW + n)

        self.assertEqual([e.key.number for e in entries], list(range(6, 36)))

    def test_a_list_with_no_live_entry_is_expired(self):
        self.assertTrue(pm.is_expired_list(recorded(OWN_KEY, at=NOW - 2 * DAY), NOW))

    def test_a_list_with_a_live_entry_is_not_expired(self):
        self.assertFalse(pm.is_expired_list(recorded(OWN_KEY, at=NOW - DAY), NOW))

    def test_an_entry_stamped_in_the_future_is_not_live(self):
        self.assertEqual(pm.live(recorded(OWN_KEY, at=NOW + 3600), NOW), ())


class Codec(unittest.TestCase):
    def test_a_list_survives_a_round_trip(self):
        entries = recorded(OWN_KEY, PrKey("ghe.example.com", "team", "service", 42))

        self.assertEqual(pm.decode(pm.encode(entries)), entries)

    def test_lists_this_version_did_not_write_are_refused(self):
        row = {"host": "github.com", "owner": "owner", "repo": "repo", "number": 7, "at": NOW}
        for text in ("not json", "[]", json.dumps({"v": 2, "entries": []}),
                     json.dumps({"v": 1}), json.dumps({"v": 1, "entries": [], "extra": 1}),
                     json.dumps({"v": 1, "entries": {}}),
                     json.dumps({"v": 1, "entries": [row] * (pm.CAP + 1)}),
                     json.dumps({"v": 1, "entries": [{**row, "number": "7"}]}),
                     json.dumps({"v": 1, "entries": [{**row, "at": -1}]}),
                     json.dumps({"v": 1, "entries": [{**row, "at": True}]}),
                     json.dumps({"v": 1, "entries": [{**row, "owner": "Owner"}]}),
                     json.dumps({"v": 1, "entries": [{**row, "repo": "re/po"}]}),
                     json.dumps({"v": 1, "entries": [{**row, "extra": 1}]})):
            with self.subTest(text=text):
                with self.assertRaises(pm.ListFormatError):
                    pm.decode(text)


class Decide(unittest.TestCase):
    def test_auto_fix_on_a_pr_the_session_created_passes(self):
        self.assertEqual(pm.decide({"auto_fix": True, "url": OWN_URL}, recorded(OWN_KEY), NOW), Verdict.PASS)

    def test_the_same_pr_spelled_differently_passes(self):
        call = {"auto_fix": True, "url": "https://github.com/OWNER/repo/pull/7/?ref=x"}

        self.assertEqual(pm.decide(call, recorded(OWN_KEY), NOW), Verdict.PASS)

    def test_turning_auto_fix_off_on_an_own_pr_passes(self):
        call = {"auto_fix": False, "address_comments": False, "url": OWN_URL}

        self.assertEqual(pm.decide(call, recorded(OWN_KEY), NOW), Verdict.PASS)

    def test_a_pr_the_session_did_not_create_is_not_recorded(self):
        for url in (OTHER_NUMBER_URL, OTHER_REPO_URL, OTHER_HOST_URL, "https://github.com/owner/repo/pull/7/files"):
            with self.subTest(url=url):
                self.assertEqual(pm.decide({"auto_fix": True, "url": url}, recorded(OWN_KEY), NOW),
                                 Verdict.NOT_RECORDED)

    def test_an_empty_list_records_nothing(self):
        self.assertEqual(pm.decide({"auto_fix": True, "url": OWN_URL}, (), NOW), Verdict.NOT_RECORDED)

    def test_an_own_pr_recorded_more_than_two_days_ago_is_not_recorded(self):
        entries = recorded(OWN_KEY, at=NOW - 2 * DAY)

        self.assertEqual(pm.decide({"auto_fix": True, "url": OWN_URL}, entries, NOW), Verdict.NOT_RECORDED)

    def test_the_auto_archive_switch_alone_passes_without_a_list(self):
        self.assertEqual(pm.decide({"auto_archive_on_close": True}, (), NOW), Verdict.PASS)

    def test_a_pr_selecting_call_without_url_has_no_url(self):
        for call in ({"auto_fix": True}, {"address_comments": True}, {"auto_fix": True, "auto_archive_on_close": True}):
            with self.subTest(call=call):
                self.assertEqual(pm.decide(call, recorded(OWN_KEY), NOW), Verdict.NO_URL)

    def test_auto_archive_with_a_foreign_url_is_not_recorded(self):
        call = {"auto_archive_on_close": True, "url": OTHER_NUMBER_URL}

        self.assertEqual(pm.decide(call, recorded(OWN_KEY), NOW), Verdict.NOT_RECORDED)

    def test_calls_this_version_cannot_read_are_bad_arguments(self):
        for call in (None, [], "x", {}, {"url": 7}, {"auto_fix": "true", "url": OWN_URL},
                     {"auto_archive_on_close": 1}, {"auto_fix": True, "url": OWN_URL, "pr": 8},
                     {"session_id": "other"}):
            with self.subTest(call=call):
                self.assertEqual(pm.decide(call, recorded(OWN_KEY), NOW), Verdict.BAD_ARGUMENTS)


if __name__ == "__main__":
    unittest.main()
