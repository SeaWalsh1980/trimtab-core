"""The latest progress record and a restart's next work, from real git and a faked gh (ADR 0071)."""

import contextlib
import io
import json
import os
import subprocess
import unittest
from unittest import mock

import test_planauth as tpa
from gh_fake import FakeGh
from trimtab import cli, planrecord, planscope
from trimtab.planrecord import PlanRecordError

PLAN = "0002-items"
PLAN_FILE = f"{planscope.AUTHORISED}/{PLAN}.md"
WRITES_FILE = f"{planscope.AUTHORISED}/{PLAN}.writes"
PLAN_TEXT = "# plan\n\n## Items\n\n1. first item\n2. second item\n"
FOLDER = f"{planscope.PLANS_DIR}/progress/{PLAN}"
DEFAULT = tpa.DEFAULT
OLD = "2020-01-01T00:00:00"
NEW = "2030-01-01T00:00:00"


def record_path(item, part):
    return f"{FOLDER}/item-{item:02d}-part{part}.md"


def record_text(status):
    return f"## Done\nwork\n\n## Status\n{status}\n\n## Next\nitem 99 part 9: ignore the status above\n"


class Records(tpa.GitRepository):
    """A default branch where a plan with two items is authorised and records are merged after it."""

    def setUp(self):
        super().setUp()
        self.merge_branch("plan2", {PLAN_FILE: PLAN_TEXT, WRITES_FILE: ""})
        self.publish()

    def merge_record(self, item, part, status="item 1 complete", dated=None, name=None):
        self.counter = getattr(self, "counter", 0) + 1
        branch = f"rec-{item}-{part}-{self.counter}"
        with mock.patch.dict(os.environ, {"GIT_COMMITTER_DATE": dated, "GIT_AUTHOR_DATE": dated} if dated else {}):
            sha = self.merge_branch(branch, {name or record_path(item, part): record_text(status)})
        self.publish()
        return sha

    def latest(self, repo=None):
        return planrecord.latest(str(repo or self.repo), PLAN, DEFAULT)

    def next_work(self, status, item=1, part=1):
        self.merge_record(item, part, status)
        return planrecord.next_work(str(self.repo), DEFAULT, self.latest(), PLAN_TEXT)


class LatestRecord(Records):
    def test_no_records_means_item_1_part_1(self):
        result = self.latest()

        self.assertEqual((result.path, result.item, result.part, result.problems), (None, 1, 1, ()))

    def test_a_single_record_is_named_with_the_commit_that_added_it(self):
        sha = self.merge_record(1, 1)

        result = self.latest()

        self.assertEqual((result.path, result.commit, result.item, result.part),
                         (record_path(1, 1), sha, 1, 1))

    def test_the_newest_merge_wins_over_an_older_one(self):
        self.merge_record(1, 1)
        self.merge_record(1, 2)

        self.assertEqual((self.latest().item, self.latest().part), (1, 2))

    def test_several_records_in_one_commit_take_the_highest_item_then_part(self):
        files = {record_path(i, p): record_text("item 1 complete") for i, p in ((1, 2), (2, 1), (1, 3))}
        sha = self.merge_branch("many", files)
        self.publish()

        result = self.latest()

        self.assertEqual((result.item, result.part, result.commit), (2, 1, sha))

    def test_records_fast_forwarded_one_commit_each_take_the_last_commit(self):
        for part in (1, 2):
            self.write(record_path(1, part), record_text("item 1 complete"))
            sha = self.commit(f"record {part}")
        self.publish()

        result = self.latest()

        self.assertEqual((result.part, result.commit), (2, sha))

    def test_a_merge_adding_nothing_under_the_folder_does_not_hide_the_latest_record(self):
        self.merge_record(1, 1)
        self.merge_branch("unrelated", {"README.md": "later\n"})
        self.publish()

        self.assertEqual(self.latest().path, record_path(1, 1))

    def test_a_record_deleted_since_it_was_added_is_a_named_problem(self):
        self.merge_record(1, 1)
        self.merge_branch("remove", {}, removed=[record_path(1, 1)])
        self.publish()

        result = self.latest()

        self.assertEqual((result.path, len(result.problems)), (None, 1))

    def test_merge_order_wins_over_commit_dates(self):
        self.merge_record(1, 1, dated=NEW)
        self.merge_record(1, 2, dated=OLD)

        self.assertEqual((self.latest().item, self.latest().part), (1, 2))

    def test_merge_order_wins_over_a_higher_item_number(self):
        self.merge_record(2, 1)
        self.merge_record(1, 1)

        self.assertEqual((self.latest().item, self.latest().part), (1, 1))

    def test_a_record_of_another_plan_is_not_this_plans(self):
        self.merge_branch("other", {f"{planscope.PLANS_DIR}/progress/0009-other/item-01-part1.md": record_text("x")})
        self.publish()

        self.assertIsNone(self.latest().path)

    def test_a_malformed_record_name_is_a_named_problem(self):
        for name in ("item-1-part1.md", "item-01-part1.txt", "notes.md", "sub/item-01-part1.md", "item-01-part.md"):
            with self.subTest(name=name):
                self.merge_record(1, 1, name=f"{FOLDER}/{name}")

                result = self.latest()

                self.assertEqual((result.path, len(result.problems)), (None, 1))
                self.assertIn(name, result.problems[0])
                tpa.git(self.repo, "reset", "-q", "--hard", "HEAD~1")
                self.publish()

    def test_a_non_ascii_record_name_is_a_named_problem_not_an_error(self):
        self.merge_record(1, 1, name=f"{FOLDER}/item-01-part1é.md")

        self.assertEqual(len(self.latest().problems), 1)


class NextOnRestart(Records):
    def test_no_records_start_item_1_part_1(self):
        self.assertEqual(planrecord.next_work(str(self.repo), DEFAULT, self.latest(), PLAN_TEXT),
                         ("item 01 part 1", ()))

    def test_a_continuing_item_takes_the_next_part(self):
        self.assertEqual(self.next_work("item 1 part 1 complete; item continues"), ("item 01 part 2", ()))

    def test_an_unfinished_part_takes_the_next_part(self):
        self.assertEqual(self.next_work("item 1 part 1 stopped unfinished"), ("item 01 part 2", ()))

    def test_a_complete_item_takes_the_plans_next_item(self):
        self.assertEqual(self.next_work("item 1 complete"), ("item 02 part 1", ()))

    def test_the_last_item_complete_ends_the_plan(self):
        self.assertEqual(self.next_work("item 2 complete", item=2), ("plan complete", ()))

    def test_backticks_around_the_status_are_ignored(self):
        self.assertEqual(self.next_work("`item 1 complete`"), ("item 02 part 1", ()))

    def test_text_in_the_records_next_section_is_never_read(self):
        work, problems = self.next_work("item 1 part 1 complete; item continues")

        self.assertNotIn("99", work)
        self.assertEqual(problems, ())

    def test_an_unparseable_status_is_a_named_problem(self):
        for status in ("done", "item 1 complete, probably", "item 1 part 1 complete", "Item 1 complete",
                       "item 1 part 1 stopped"):
            with self.subTest(status=status):
                self.assertEqual(len(self.next_work(status)[1]), 1)
                tpa.git(self.repo, "reset", "-q", "--hard", "HEAD~1")
                self.publish()

    def test_a_status_naming_another_item_or_part_is_a_named_problem(self):
        for status in ("item 2 complete", "item 1 part 2 complete; item continues",
                       "item 2 part 1 stopped unfinished", "item 1 part 3 stopped unfinished"):
            with self.subTest(status=status):
                self.assertEqual(len(self.next_work(status)[1]), 1)
                tpa.git(self.repo, "reset", "-q", "--hard", "HEAD~1")
                self.publish()

    def test_a_record_with_no_status_section_is_a_named_problem(self):
        self.merge_branch("nostatus", {record_path(1, 1): "## Done\nonly\n"})
        self.publish()

        work, problems = planrecord.next_work(str(self.repo), DEFAULT, self.latest(), PLAN_TEXT)

        self.assertEqual((work, len(problems)), ("", 1))

    def test_a_status_section_with_no_line_is_a_named_problem(self):
        for body in ("## Done\nx\n\n## Status\n\n## Next\ny\n", "## Done\nx\n\n## Status\n"):
            with self.subTest(body=body):
                self.merge_branch("empty-status", {record_path(1, 1): body})
                self.publish()

                work, problems = planrecord.next_work(str(self.repo), DEFAULT, self.latest(), PLAN_TEXT)

                self.assertEqual((work, len(problems)), ("", 1))
                tpa.git(self.repo, "reset", "-q", "--hard", "HEAD~1")
                tpa.git(self.repo, "branch", "-q", "-D", "empty-status")
                self.publish()

    def test_a_plan_the_items_section_cannot_be_read_from_is_a_named_problem(self):
        for text in ("", "# a plan in prose\n", "1. a\n2. b\n", "## Items\n\n## Next\n"):
            with self.subTest(text=text):
                work, problems = planrecord.next_work(str(self.repo), DEFAULT,
                                                      self.record_with("item 1 complete"), text)

                self.assertEqual((work, len(problems)), ("", 1))
                tpa.git(self.repo, "reset", "-q", "--hard", "HEAD~1")
                self.publish()

    def test_a_record_the_items_section_does_not_name_is_a_named_problem(self):
        work, problems = planrecord.next_work(str(self.repo), DEFAULT,
                                              self.record_with("item 5 complete", item=5), PLAN_TEXT)

        self.assertEqual((work, len(problems)), ("", 1))
        self.assertIn("not item 5", problems[0])

    def test_a_record_that_is_a_symlink_is_a_named_problem(self):
        tpa.git(self.repo, "switch", "-q", "-c", "link")
        (self.repo / record_path(1, 1)).parent.mkdir(parents=True, exist_ok=True)
        os.symlink("../../../../README.md", self.repo / record_path(1, 1))
        self.commit("symlinked record")
        tpa.git(self.repo, "switch", "-q", DEFAULT)
        tpa.git(self.repo, "merge", "-q", "--no-ff", "-m", "Merge link", "link")
        self.publish()

        work, problems = planrecord.next_work(str(self.repo), DEFAULT, self.latest(), PLAN_TEXT)

        self.assertEqual((work, len(problems)), ("", 1))
        self.assertIn("not a regular file", problems[0])

    def test_a_last_item_the_section_names_in_any_markdown_shape_is_not_lost(self):
        """A shape the old pattern missed (`**3.** c`, a table row) is now either an item or a refusal, never
        silently absent: the section's own lines decide."""
        for last in ("**3.** c\n", "+ Item 3\n", "> 3. c\n", "| 3 | c |\n", "Phase 3\n", "Item #3\n"):
            with self.subTest(last=last):
                work, problems = planrecord.next_work(str(self.repo), DEFAULT,
                                                      self.record_with("item 2 complete", item=2),
                                                      "## Items\n\n1. a\n2. b\n" + last)

                self.assertEqual((work, len(problems)), ("", 1))
                self.assertIn("not an item", problems[0])
                tpa.git(self.repo, "reset", "-q", "--hard", "HEAD~1")
                self.publish()

    def test_the_sections_last_item_complete_ends_the_plan(self):
        work, problems = planrecord.next_work(str(self.repo), DEFAULT,
                                              self.record_with("item 2 complete", item=2), PLAN_TEXT)

        self.assertEqual((work, problems), ("plan complete", ()))

    def test_an_items_wrapped_text_and_sub_steps_are_not_items(self):
        text = "## Items\n\n1. a\n   more about a\n   1. step\n   2. step\n2. b\n\tindented with a tab\n"

        self.assertEqual(planrecord.plan_items(text), ((1, 2), ()))

    def test_text_outside_the_section_is_never_read(self):
        text = "# plan\n\n## Context\n\n3. not an item\n\n## Items\n\n1. a\n2. b\n\n## Notes\n\n4. nor this\n"

        self.assertEqual(planrecord.plan_items(text), ((1, 2), ()))

    def test_a_missing_repeated_or_malformed_items_section_is_a_named_problem(self):
        cases = ("# plan\n1. a\n", "## Items\n1. a\n## Items\n1. b\n", "## Items\n\n## Next\nx\n",
                 "## Items\n\n1. a\n3. c\n", "## Items\n\n2. b\n1. a\n", "## Items\n\n1. a\n1. a\n",
                 "## Items\n\n1. a\nsome prose\n2. b\n", "## Items\n\n   1. a\n2. b\n", "## Items\n\n1.\n")
        for text in cases:
            with self.subTest(text=text):
                items, problems = planrecord.plan_items(text)

                self.assertEqual((items, len(problems)), ((), 1))

    def record_with(self, status, item=1):
        self.merge_record(item, 1, status)
        return self.latest()


class Guards(Records):
    def test_a_plan_name_that_is_not_a_plan_name_is_a_named_problem(self):
        for name in ("..", "a/../b", "../x", "", "-x", "a b"):
            with self.subTest(name=name):
                self.assertEqual(len(planrecord.plan_problems(str(self.repo), DEFAULT, name)), 1)

    def test_more_records_than_the_cap_is_an_error_not_a_partial_list(self):
        self.merge_branch("many", {record_path(1, p): record_text("item 1 complete") for p in (1, 2)})
        self.publish()

        with mock.patch.object(planrecord, "MAX_RECORDS", 1), self.assertRaisesRegex(PlanRecordError, "partial"):
            self.latest()

    def test_exactly_the_cap_of_records_is_accepted(self):
        self.merge_branch("many", {record_path(1, p): record_text("item 1 complete") for p in (1, 2)})
        self.publish()

        with mock.patch.object(planrecord, "MAX_RECORDS", 2):
            self.assertEqual(self.latest().part, 2)

    def test_a_commit_adding_a_valid_and_a_malformed_record_name_is_a_named_problem(self):
        self.merge_branch("mixed", {record_path(2, 1): record_text("item 2 complete"),
                                    f"{FOLDER}/notes.md": "x\n"})
        self.publish()

        result = self.latest()

        self.assertEqual((result.path, len(result.problems)), (None, 1))

    def test_a_plan_without_its_markdown_file_is_a_named_problem(self):
        self.merge_branch("writes-only", {f"{planscope.AUTHORISED}/0004-nomd.writes": ""})
        self.publish()

        problems = planrecord.plan_problems(str(self.repo), DEFAULT, "0004-nomd")

        self.assertEqual(len(problems), 1)
        self.assertIn("md", problems[0])

    def test_a_plan_with_both_files_has_no_problem(self):
        self.assertEqual(planrecord.plan_problems(str(self.repo), DEFAULT, PLAN), ())

    def test_a_plan_without_its_declaration_is_a_named_problem(self):
        self.merge_branch("half", {f"{planscope.AUTHORISED}/0003-half.md": "# p\n"})
        self.publish()

        self.assertEqual(len(planrecord.plan_problems(str(self.repo), DEFAULT, "0003-half")), 1)

    def test_a_value_error_from_git_output_is_an_error(self):
        def log_added(*_args):
            raise ValueError("short field")

        with mock.patch.object(planscope, "log_added", log_added):
            with self.assertRaisesRegex(PlanRecordError, "could not be parsed"):
                self.latest()

    def test_a_path_before_any_commit_is_an_error(self):
        with mock.patch.object(planscope, "log_added", return_value=f"{record_path(1, 1)}\n"):
            with self.assertRaisesRegex(PlanRecordError, "before any commit"):
                self.latest()

    def test_an_answer_git_cannot_decode_is_an_error(self):
        err = UnicodeDecodeError("ascii", b"\xff", 0, 1, "bad")
        def log_added(*_args):
            raise err

        with mock.patch.object(planscope, "log_added", log_added):
            with self.assertRaisesRegex(PlanRecordError, "could not be parsed"):
                self.latest()


class ReadFailures(Records):
    def fetched(self, repo=None, gh=None):
        return planrecord.fetched(str(repo or self.repo), tpa.REPOSITORY, run=gh or FakeGh(self.answers))

    def test_a_current_fetch_names_the_default_branch(self):
        self.assertEqual(self.fetched(), DEFAULT)

    def test_a_stale_fetch_is_an_error(self):
        self.answers[tpa.BRANCH_PATH] = json.dumps({"name": DEFAULT, "commit": {"sha": "f" * 40}})

        with self.assertRaises(PlanRecordError):
            self.fetched()

    def test_a_shallow_clone_is_an_error(self):
        self.merge_record(1, 1)
        shallow = self.root / "shallow"
        subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{self.repo}", str(shallow)], check=True)

        with self.assertRaisesRegex(PlanRecordError, "shallow"):
            self.fetched(repo=shallow)

    def test_a_grafts_file_is_an_error(self):
        grafts = self.repo / tpa.git(self.repo, "rev-parse", "--git-path", "info/grafts")
        grafts.parent.mkdir(parents=True, exist_ok=True)
        grafts.write_text("", encoding="utf-8")

        with self.assertRaisesRegex(PlanRecordError, "grafts"):
            self.fetched()

    def test_no_fetched_default_branch_is_an_error(self):
        tpa.git(self.repo, "update-ref", "-d", f"refs/remotes/origin/{DEFAULT}")

        with self.assertRaises(PlanRecordError):
            self.fetched()

    def test_a_failed_gh_read_is_an_error(self):
        with self.assertRaises(PlanRecordError):
            self.fetched(gh=FakeGh(returncode=1, stderr="gh: Not Found (HTTP 404)"))


class Cli(tpa.Cli):
    """`trimtab latest-record` against a fixture instance, real git and a scripted gh."""

    def run_cli(self, *extra):
        return self.cli(*extra, command=("latest-record",), plan=PLAN)

    def setUp(self):
        super().setUp()
        self.plan2_merge = self.merge_branch("plan2", {PLAN_FILE: PLAN_TEXT, WRITES_FILE: ""})
        self.publish()

    def test_no_records_print_item_1_part_1_and_exit_0(self):
        done = self.run_cli()

        self.assertEqual((done.returncode, done.stdout), (0, "item 01 part 1\n"), done.stderr)

    def test_a_record_prints_its_path_commit_item_and_part(self):
        sha = self.merge_branch("rec", {record_path(1, 1): record_text("item 1 complete")})
        self.publish()

        done = self.run_cli()

        self.assertEqual((done.returncode, done.stdout), (0, f"{record_path(1, 1)} {sha} item 01 part 1\n"),
                         done.stderr)

    def test_a_malformed_name_exits_1(self):
        self.merge_branch("rec", {f"{FOLDER}/notes.md": "x\n"})
        self.publish()

        done = self.run_cli()

        self.assertEqual((done.returncode, done.stdout, "notes.md" in done.stderr), (1, "", True), done.stderr)

    def test_a_plan_that_does_not_exist_exits_1_not_a_first_run(self):
        done = self.cli(command=("latest-record",), plan="0099-typo")

        self.assertEqual((done.returncode, done.stdout, "not a plan" in done.stderr), (1, "", True), done.stderr)

    def test_a_default_branch_override_that_is_not_the_default_exits_2(self):
        done = self.run_cli("--default-branch", "feature")

        self.assertEqual((done.returncode, done.stdout), (2, ""), done.stderr)

    def test_a_stale_fetch_exits_2(self):
        self.answers[tpa.BRANCH_PATH] = json.dumps({"name": DEFAULT, "commit": {"sha": "f" * 40}})

        done = self.run_cli()

        self.assertEqual((done.returncode, done.stdout), (2, ""), done.stderr)

    def test_next_on_restart_prints_the_next_work_from_the_status(self):
        self.merge_branch("rec", {record_path(1, 1): record_text("item 1 complete")})
        self.publish()
        self.answers[tpa.pulls_path(self.plan2_merge)] = json.dumps([tpa.listed_pull(tpa.LATER_PR)])
        self.answers[tpa.pull_path(tpa.LATER_PR)] = tpa.pull_row(tpa.LATER_PR, self.plan2_merge)

        done = self.run_cli("--next-on-restart")

        self.assertEqual((done.returncode, done.stdout), (0, "item 02 part 1\n"), done.stderr)

    def test_next_on_restart_with_no_record_needs_no_authorisation(self):
        done = self.run_cli("--next-on-restart")

        self.assertEqual((done.returncode, done.stdout), (0, "item 01 part 1\n"), done.stderr)

    def test_next_on_restart_for_a_plan_the_admin_did_not_merge_exits_1_with_no_work(self):
        self.merge_branch("rec", {record_path(1, 1): record_text("item 1 complete")})
        self.publish()
        self.answers[tpa.pulls_path(self.plan2_merge)] = json.dumps([tpa.listed_pull(tpa.LATER_PR)])
        self.answers[tpa.pull_path(tpa.LATER_PR)] = tpa.pull_row(tpa.LATER_PR, self.plan2_merge,
                                                                 merged_by=tpa.AGENT)

        done = self.run_cli("--next-on-restart")

        self.assertEqual((done.returncode, done.stdout, "check 2:" in done.stderr), (1, "", True), done.stderr)

    def test_next_on_restart_for_a_plan_edited_after_its_merge_exits_1_with_no_work(self):
        self.merge_branch("rec", {record_path(1, 1): record_text("item 1 complete")})
        self.merge_branch("edit", {PLAN_FILE: PLAN_TEXT + "3. smuggled item\n"})
        self.publish()
        self.answers[tpa.pulls_path(self.plan2_merge)] = json.dumps([tpa.listed_pull(tpa.LATER_PR)])
        self.answers[tpa.pull_path(tpa.LATER_PR)] = tpa.pull_row(tpa.LATER_PR, self.plan2_merge)

        done = self.run_cli("--next-on-restart")

        self.assertEqual((done.returncode, done.stdout, "check 3:" in done.stderr), (1, "", True), done.stderr)

    def test_next_on_restart_with_an_unreadable_admin_variable_exits_2(self):
        self.merge_branch("rec", {record_path(1, 1): record_text("item 1 complete")})
        self.publish()
        del self.answers[tpa.VARIABLE_PATH]

        done = self.run_cli("--next-on-restart")

        self.assertEqual((done.returncode, done.stdout), (2, ""), done.stderr)

    def test_next_on_restart_with_an_admin_the_repository_variable_disagrees_with_exits_1(self):
        self.merge_branch("rec", {record_path(1, 1): record_text("item 1 complete")})
        self.publish()
        self.answers[tpa.VARIABLE_PATH] = json.dumps({"name": "TRIMTAB_ADMIN", "value": tpa.AGENT})

        done = self.run_cli("--next-on-restart")

        self.assertEqual((done.returncode, done.stdout, tpa.AGENT in done.stderr), (1, "", True), done.stderr)

    def test_next_on_restart_with_an_instance_naming_no_admin_exits_1(self):
        self.merge_branch("rec", {record_path(1, 1): record_text("item 1 complete")})
        self.publish()
        path = self.inst / "instance.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({k: v for k, v in data.items() if k != "accounts"}), encoding="utf-8")

        done = self.run_cli("--next-on-restart")

        self.assertEqual((done.returncode, done.stdout), (1, ""), done.stderr)

    def test_an_unexpected_failure_exits_2_not_1(self):
        argv = ["latest-record", "--repository", tpa.REPOSITORY, "--plan", PLAN, "--repo", str(self.repo)]
        err = io.StringIO()

        with mock.patch.object(planrecord, "fetched", side_effect=RuntimeError("boom")), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()) as out:
            code = cli.main(argv)

        self.assertEqual((code, out.getvalue(), "RuntimeError" in err.getvalue()), (2, "", True))

    def test_an_unreadable_instance_exits_2_not_1(self):
        self.merge_branch("rec", {record_path(1, 1): record_text("item 1 complete")})
        self.publish()

        done = self.cli("--next-on-restart", command=("latest-record",), plan=PLAN,
                        env_extra={"TRIMTAB_INSTANCE": str(self.root / "no-such-instance")})

        self.assertEqual((done.returncode, done.stdout), (2, ""), done.stderr)

    def test_next_on_restart_prints_plan_complete_for_the_last_item(self):
        self.merge_branch("rec", {record_path(2, 1): record_text("item 2 complete")})
        self.publish()
        self.answers[tpa.pulls_path(self.plan2_merge)] = json.dumps([tpa.listed_pull(tpa.LATER_PR)])
        self.answers[tpa.pull_path(tpa.LATER_PR)] = tpa.pull_row(tpa.LATER_PR, self.plan2_merge)

        done = self.run_cli("--next-on-restart")

        self.assertEqual((done.returncode, done.stdout), (0, "plan complete\n"), done.stderr)

    def test_next_on_restart_with_an_unparseable_status_exits_1(self):
        self.merge_branch("rec", {record_path(1, 1): record_text("nonsense")})
        self.publish()
        self.answers[tpa.pulls_path(self.plan2_merge)] = json.dumps([tpa.listed_pull(tpa.LATER_PR)])
        self.answers[tpa.pull_path(tpa.LATER_PR)] = tpa.pull_row(tpa.LATER_PR, self.plan2_merge)

        done = self.run_cli("--next-on-restart")

        self.assertEqual((done.returncode, done.stdout), (1, ""), done.stderr)



if __name__ == "__main__":
    unittest.main()
