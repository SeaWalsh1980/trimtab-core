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
from trimtab import cli, planauth, planrecord, planscope
from trimtab.capture.sources import GhApi
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

    def tip_of(self, repo=None):
        """The commit origin's default branch holds, as `fetched` resolves and checks it."""
        return tpa.git(repo or self.repo, "rev-parse", f"refs/remotes/origin/{DEFAULT}")

    def latest(self, repo=None):
        return planrecord.latest(str(repo or self.repo), self.tip_of(repo), PLAN)

    def next_work(self, status, item=1, part=1):
        self.merge_record(item, part, status)
        return planrecord.next_work(str(self.repo), self.tip_of(), self.latest(), PLAN_TEXT)


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
        self.assertEqual(planrecord.next_work(str(self.repo), self.tip_of(), self.latest(), PLAN_TEXT),
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

    def test_a_records_status_heading_is_read_as_strictly_as_the_plans(self):
        """The record's prose is the least trusted input here: a status the rendered record does not show must
        not decide the next work."""
        bodies = {
            "a heading only a form feed opens": "## Done\nx\x0c## Status\x0citem 1 complete\n",
            "an indented heading": "## Done\nx\n\n  ## Status\n  item 1 complete\n",
            "a fenced example above the real one": "## Done\n```\n## Status\nitem 1 part 1 complete; item "
                                                   "continues\n```\n\n## Status\nitem 1 complete\n",
            "a carriage return inside a line": "## Done\nx\n\n## Status\r item 1 complete\n",
        }
        for name, body in bodies.items():
            with self.subTest(name=name):
                self.merge_branch("status-shape", {record_path(1, 1): body})
                self.publish()

                work, problems = planrecord.next_work(str(self.repo), self.tip_of(), self.latest(), PLAN_TEXT)

                self.assertEqual((work, len(problems)), ("", 1))
                tpa.git(self.repo, "reset", "-q", "--hard", "HEAD~1")
                tpa.git(self.repo, "branch", "-q", "-D", "status-shape")
                self.publish()

    def test_a_status_heading_is_accepted_in_the_same_forms_as_the_plans(self):
        for heading in ("## Status", "## status", "##\tStatus", "## Status   "):
            with self.subTest(heading=heading):
                self.merge_branch("status-form", {record_path(1, 1): f"## Done\nx\n\n{heading}\nitem 1 complete\n"})
                self.publish()

                work, problems = planrecord.next_work(str(self.repo), self.tip_of(), self.latest(), PLAN_TEXT)

                self.assertEqual((work, problems), ("item 02 part 1", ()))
                tpa.git(self.repo, "reset", "-q", "--hard", "HEAD~1")
                tpa.git(self.repo, "branch", "-q", "-D", "status-form")
                self.publish()

    def test_a_lower_case_decoy_heading_counts_as_a_second_status_heading(self):
        """Matching without case is the refusing choice here: a decoy is counted, not ignored."""
        self.merge_branch("decoy", {record_path(1, 1): "## status\nitem 1 part 1 complete; item continues\n\n"
                                                       "## Status\nitem 1 complete\n"})
        self.publish()

        work, problems = planrecord.next_work(str(self.repo), self.tip_of(), self.latest(), PLAN_TEXT)

        self.assertEqual((work, len(problems)), ("", 1))
        self.assertIn("2 ## Status headings", problems[0])

    def test_an_indented_heading_anywhere_in_the_record_is_a_named_problem(self):
        """Markdown may read it as a ## Status this does not see."""
        self.merge_branch("indented", {record_path(1, 1): "## Done\nx\n  ## Status\n\n## Status\nitem 1 complete\n"})
        self.publish()

        work, problems = planrecord.next_work(str(self.repo), self.tip_of(), self.latest(), PLAN_TEXT)

        self.assertEqual((work, len(problems)), ("", 1))
        self.assertIn("indented heading", problems[0])

    def test_a_heading_behind_a_blockquote_or_list_marker_is_a_named_problem(self):
        """Markdown renders each as a heading the reader would not see, so none may sit anywhere in a record."""
        prefixes = (">", "> >", "  >", "-", "*", "+", "1.", "2)", "- >", "> -", "   -", "1. 2.")
        for prefix in prefixes:
            with self.subTest(prefix=prefix):
                self.merge_branch("prefixed", {record_path(1, 1): f"## Done\nx\n{prefix} ## Status\n\n## Status\n"
                                                                  "item 1 complete\n"})
                self.publish()

                work, problems = planrecord.next_work(str(self.repo), self.tip_of(), self.latest(), PLAN_TEXT)

                self.assertEqual((work, len(problems)), ("", 1))
                self.assertIn("blockquote or list marker", problems[0])
                tpa.git(self.repo, "reset", "-q", "--hard", "HEAD~1")
                tpa.git(self.repo, "branch", "-q", "-D", "prefixed")
                self.publish()

    def test_a_blockquote_with_no_space_before_the_hashes_is_refused(self):
        self.merge_branch("nospace", {record_path(1, 1): "## Done\n>## Status\n\n## Status\nitem 1 complete\n"})
        self.publish()

        work, problems = planrecord.next_work(str(self.repo), self.tip_of(), self.latest(), PLAN_TEXT)

        self.assertEqual((work, len(problems)), ("", 1))

    def test_prefixed_lines_that_are_not_headings_do_not_trip_the_refusal(self):
        """A marker with no heading after it, a hash with no blank after it, and a marker with no space are text."""
        body = ("## Done\n- #123 fixed\n> a plain quote\n- an item\n1. another\n-## not a list\n"
                "The line reads `## Status` then `item 1 complete`.\n\n## Status\nitem 1 complete\n")
        self.merge_branch("plain", {record_path(1, 1): body})
        self.publish()

        self.assertEqual(planrecord.next_work(str(self.repo), self.tip_of(), self.latest(), PLAN_TEXT),
                         ("item 02 part 1", ()))

    def test_a_heading_named_status_in_a_form_the_reader_does_not_recognise_is_refused(self):
        """Placed ahead of the real heading it would show a reviewer a different status from the one printed."""
        for decoy in ("# Status", "### Status", "## Status ##", "## status #", "Status\n---", "Status\n===",
                      "<h2>Status</h2>"):
            with self.subTest(decoy=decoy):
                self.merge_branch("decoy-form", {record_path(1, 1): f"## Done\n{decoy}\nitem 1 complete\n\n"
                                                                    "## Status\nitem 1 part 1 stopped unfinished\n"})
                self.publish()

                work, problems = planrecord.next_work(str(self.repo), self.tip_of(), self.latest(), PLAN_TEXT)

                self.assertEqual((work, len(problems)), ("", 1))
                self.assertRegex(problems[0], r"heading|underline")
                tpa.git(self.repo, "reset", "-q", "--hard", "HEAD~1")
                tpa.git(self.repo, "branch", "-q", "-D", "decoy-form")
                self.publish()

    def test_a_status_decoy_in_emphasis_behind_a_quote_or_in_a_broken_tag_is_refused(self):
        for decoy in ("## *Status*", "## **Status**", "> Status\n> ---", "**Status**\n===", "<h3\n>Status</h3>"):
            with self.subTest(decoy=decoy):
                self.merge_branch("decoy-quote", {record_path(1, 1): f"## Done\n{decoy}\nitem 1 complete\n\n"
                                                                     "## Status\nitem 1 part 1 stopped unfinished\n"})
                self.publish()

                work, problems = planrecord.next_work(str(self.repo), self.tip_of(), self.latest(), PLAN_TEXT)

                self.assertEqual((work, len(problems)), ("", 1))
                tpa.git(self.repo, "reset", "-q", "--hard", "HEAD~1")
                tpa.git(self.repo, "branch", "-q", "-D", "decoy-quote")
                self.publish()

    def test_plain_words_status_in_prose_are_still_read(self):
        body = "## Done\nStatus updates are in the log. The status of the work is fine.\n\n## Status\nitem 1 complete\n"
        self.merge_branch("prose", {record_path(1, 1): body})
        self.publish()

        self.assertEqual(planrecord.next_work(str(self.repo), self.tip_of(), self.latest(), PLAN_TEXT),
                         ("item 02 part 1", ()))

    def test_a_second_line_under_status_is_a_named_problem(self):
        self.merge_branch("twoline", {record_path(1, 1): "## Done\nx\n\n## Status\nitem 1 complete\nitem 2 "
                                                         "complete\n"})
        self.publish()

        work, problems = planrecord.next_work(str(self.repo), self.tip_of(), self.latest(), PLAN_TEXT)

        self.assertEqual((work, len(problems)), ("", 1))
        self.assertIn("more than one line", problems[0])

    def test_a_crlf_record_still_reads_its_status(self):
        self.merge_branch("crlf", {record_path(1, 1): "## Done\r\nx\r\n\r\n## Status\r\nitem 1 complete\r\n"})
        self.publish()

        self.assertEqual(planrecord.next_work(str(self.repo), self.tip_of(), self.latest(), PLAN_TEXT),
                         ("item 02 part 1", ()))

    def test_a_record_with_no_status_section_is_a_named_problem(self):
        self.merge_branch("nostatus", {record_path(1, 1): "## Done\nonly\n"})
        self.publish()

        work, problems = planrecord.next_work(str(self.repo), self.tip_of(), self.latest(), PLAN_TEXT)

        self.assertEqual((work, len(problems)), ("", 1))

    def test_a_status_section_with_no_line_is_a_named_problem(self):
        for body in ("## Done\nx\n\n## Status\n\n## Next\ny\n", "## Done\nx\n\n## Status\n"):
            with self.subTest(body=body):
                self.merge_branch("empty-status", {record_path(1, 1): body})
                self.publish()

                work, problems = planrecord.next_work(str(self.repo), self.tip_of(), self.latest(), PLAN_TEXT)

                self.assertEqual((work, len(problems)), ("", 1))
                tpa.git(self.repo, "reset", "-q", "--hard", "HEAD~1")
                tpa.git(self.repo, "branch", "-q", "-D", "empty-status")
                self.publish()

    def test_a_plan_the_items_section_cannot_be_read_from_is_a_named_problem(self):
        for text in ("", "# a plan in prose\n", "1. a\n2. b\n", "## Items\n\n## Next\n"):
            with self.subTest(text=text):
                work, problems = self.work_after(self.record_with("item 1 complete"), text)

                self.assertEqual((work, len(problems)), ("", 1))
                tpa.git(self.repo, "reset", "-q", "--hard", "HEAD~1")
                self.publish()

    def test_a_record_the_items_section_does_not_name_is_a_named_problem(self):
        work, problems = self.work_after(self.record_with("item 5 complete", item=5), PLAN_TEXT)

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

        work, problems = planrecord.next_work(str(self.repo), self.tip_of(), self.latest(), PLAN_TEXT)

        self.assertEqual((work, len(problems)), ("", 1))
        self.assertIn("not a regular file", problems[0])

    def test_a_last_item_the_section_names_in_any_markdown_shape_is_not_lost(self):
        """A shape the old pattern missed (`**3.** c`, a table row) is now either an item or a refusal, never
        silently absent: the section's own lines decide."""
        for last in ("**3.** c\n", "+ Item 3\n", "> 3. c\n", "| 3 | c |\n", "Phase 3\n", "Item #3\n"):
            with self.subTest(last=last):
                work, problems = self.work_after(self.record_with("item 2 complete", item=2), "## Items\n\n1. a\n2. b\n" + last)

                self.assertEqual((work, len(problems)), ("", 1))
                self.assertIn("not an item", problems[0])
                tpa.git(self.repo, "reset", "-q", "--hard", "HEAD~1")
                self.publish()

    def test_the_sections_last_item_complete_ends_the_plan(self):
        work, problems = self.work_after(self.record_with("item 2 complete", item=2), PLAN_TEXT)

        self.assertEqual((work, problems), ("plan complete", ()))

    def test_an_items_wrapped_text_and_sub_steps_are_not_items(self):
        text = "## Items\n\n1. a\n   more about a\n    1. step\n    2. step\n2. b\n\tindented with a tab\n"

        self.assertEqual(planrecord.plan_items(text), ((1, 2), ()))

    def test_a_shallowly_indented_numbered_line_is_a_named_problem_not_a_sub_step(self):
        """Markdown reads a line indented less than four spaces as an item of the same list, so skipping it as
        wrapped text would hide a later item and end the plan early."""
        for last in (" 3. c\n", "  3. c\n", "   3. c\n", " 3) c\n"):
            with self.subTest(last=last):
                items, problems = planrecord.plan_items("## Items\n\n1. a\n2. b\n" + last)

                self.assertEqual((items, len(problems)), ((), 1))
                self.assertIn("less than 4 spaces", problems[0])

    def test_a_record_whose_item_a_shallow_indent_hides_never_ends_the_plan(self):
        work, problems = self.work_after(self.record_with("item 2 complete", item=2), "## Items\n\n1. a\n2. b\n  3. c\n")

        self.assertEqual((work, len(problems)), ("", 1))

    def test_the_heading_is_read_without_case_and_with_trailing_blanks(self):
        for heading in ("## Items", "## items", "##\tItems", "## Items   "):
            with self.subTest(heading=heading):
                self.assertEqual(planrecord.plan_items(f"{heading}\n1. a\n"), ((1,), ()))

    def test_a_line_that_is_not_the_heading_does_not_open_a_section(self):
        """Text that renders as plain text, not as a heading named Items, is just not the heading."""
        for heading in ("##Items", "## Items and more", "#Items"):
            with self.subTest(heading=heading):
                items, problems = planrecord.plan_items(f"{heading}\n1. a\n")

                self.assertEqual((items, len(problems)), ((), 1))
                self.assertIn("no ## Items section", problems[0])

    def test_a_heading_named_items_in_a_form_the_reader_does_not_recognise_is_refused(self):
        """It renders as a second Items heading, so the reader refuses rather than count only the real one."""
        for decoy in ("# Items", "### Items", "## Items ##", "## items #", "Items\n---", "Items\n===",
                      "  Items\n  ---", "<h2>Items</h2>", "<H3 id=x>Items</H3>"):
            with self.subTest(decoy=decoy):
                items, problems = planrecord.plan_items(f"{decoy}\n\n## Items\n\n1. a\n2. b\n")

                self.assertEqual((items, len(problems)), ((), 1))
                self.assertRegex(problems[0], r"heading|underline")

    def test_a_decoy_in_emphasis_behind_a_quote_or_in_a_broken_tag_is_refused(self):
        """Each renders as an Items heading: emphasis around the word, a setext heading inside a blockquote, an
        HTML heading tag split across lines, and a tag followed by anything."""
        for decoy in ("## *Items*", "## **Items**", "## _Items_ ##", "> Items\n> ---", ">Items\n>===", "> > Items\n> > ---",
                      "**Items**\n---", "<h2\n>Items</h2>", "<h2>Items", "<h1"):
            with self.subTest(decoy=decoy):
                items, problems = planrecord.plan_items(f"{decoy}\n\n## Items\n\n1. a\n2. b\n")

                self.assertEqual((items, len(problems)), ((), 1))
                self.assertRegex(problems[0], r"heading|underline")

    def test_a_quoted_or_emphasised_word_that_is_not_a_heading_is_still_read(self):
        """The word in a quote or in emphasis is prose unless a heading marker or an underline makes it one."""
        text = "> Items are listed below\n> Items\n> and more\n**Items** and *Items*\n\n## Items\n\n1. a\n2. b\n"

        self.assertEqual(planrecord.plan_items(text), ((1, 2), ()))

    def test_such_a_decoy_after_the_real_section_is_refused_too(self):
        items, problems = planrecord.plan_items("## Items\n\n1. a\n\n## Notes ##\n\n### Items\n")

        self.assertEqual((items, len(problems)), ((), 1))

    def test_an_indented_items_heading_outside_the_section_is_a_named_problem(self):
        """Before the real section it renders as a second Items list this reader would never count."""
        for text in ("# p\n  ## Items\n\n1. x\n2. y\n\n## Items\n\n1. a\n",
                     "## Items\n\n1. a\n\n## Notes\n   ## Items\n\n2. b\n"):
            with self.subTest(text=text):
                items, problems = planrecord.plan_items(text)

                self.assertEqual((items, len(problems)), ((), 1))
                self.assertIn("indented heading", problems[0])

    def test_the_exact_items_heading_and_plain_words_are_still_read(self):
        """The refusal is for decoys, not for the word: prose mentioning items, or Items inside a sentence."""
        text = "# Items to do\nThe items are below, see Items.\nItems and more\n\n## Items\n\n1. a\n2. b\n"

        self.assertEqual(planrecord.plan_items(text), ((1, 2), ()))

    def test_a_heading_deeper_than_the_section_is_refused_not_treated_as_content(self):
        for inner in ("### Phase one\n", "# Title\n"):
            with self.subTest(inner=inner):
                items, problems = planrecord.plan_items("## Items\n\n1. a\n" + inner + "2. b\n")

                self.assertEqual((items, len(problems)), ((), 1))
                self.assertIn("not an item", problems[0])

    def test_an_items_heading_inside_a_code_fence_counts_as_a_second_heading(self):
        text = "## Items\n\n1. a\n\n## Format\n\n```\n## Items\n1. example\n```\n"

        items, problems = planrecord.plan_items(text)

        self.assertEqual((items, len(problems)), ((), 1))
        self.assertIn("2 ## Items headings", problems[0])

    def test_carriage_returns_and_other_separators_do_not_change_the_items(self):
        for text in ("## Items\r\n\r\n1. a\r\n2. b\r\n", "## Items\n\n1. a\x0b2. hidden\n2. b\n"):
            with self.subTest(text=text):
                items, _ = planrecord.plan_items(text)

                self.assertEqual(items, (1, 2))

    def test_a_heading_behind_a_blockquote_or_list_marker_anywhere_in_the_plan_is_a_named_problem(self):
        """`> ## Items` before the section is a second heading this cannot see; `1. ## x` passes as item text."""
        cases = ("> ## Items\n\n## Items\n\n1. a\n", "## Items\n\n1. ## a\n2. b\n", "- ## Notes\n\n## Items\n\n1. a\n",
                 "## Items\n\n1. a\n   > ## Notes\n2. b\n", "## Items\n\n1. a\n> > ## Notes\n")
        for text in cases:
            with self.subTest(text=text):
                items, problems = planrecord.plan_items(text)

                self.assertEqual((items, len(problems)), ((), 1))
                self.assertIn("blockquote or list marker", problems[0])

    def test_plain_lines_in_a_plan_that_merely_start_with_a_marker_are_still_read(self):
        text = "# plan\n> a plain quote about #123\n\n## Items\n\n1. a #1 thing\n2. b\n"

        self.assertEqual(planrecord.plan_items(text), ((1, 2), ()))

    def test_a_bare_carriage_return_inside_a_line_is_a_named_problem(self):
        """Markdown reads it as a line ending, so the item after it would be invisible here."""
        items, problems = planrecord.plan_items("## Items\n\n1. a\n2. b\r3. hidden\n")

        self.assertEqual((items, len(problems)), ((), 1))
        self.assertIn("carriage return", problems[0])

    def test_an_empty_numbered_marker_indented_shallowly_is_a_named_problem(self):
        items, problems = planrecord.plan_items("## Items\n\n1. a\n2. b\n   3.\n")

        self.assertEqual((items, len(problems)), ((), 1))
        self.assertIn("less than 4 spaces", problems[0])

    def test_an_indented_heading_in_the_section_is_a_named_problem(self):
        items, problems = planrecord.plan_items("## Items\n\n1. a\n  ## Notes\n2. b\n")

        self.assertEqual((items, len(problems)), ((), 1))
        self.assertIn("indented heading", problems[0])

    def test_a_tab_indented_sub_step_is_read_as_a_sub_step(self):
        self.assertEqual(planrecord.plan_items("## Items\n\n1. a\n\t3. step\n2. b\n"), ((1, 2), ()))

    def test_a_continuing_part_of_an_item_the_plan_does_not_name_is_a_named_problem(self):
        work, problems = self.work_after(self.record_with("item 9 part 1 complete; item continues", item=9),
                                         PLAN_TEXT)

        self.assertEqual((work, len(problems)), ("", 1))
        self.assertIn("not item 9", problems[0])

    def test_a_continuing_part_is_refused_when_the_items_section_cannot_be_read(self):
        work, problems = self.work_after(self.record_with("item 1 part 1 complete; item continues"),
                                         "# plan with no items section\n")

        self.assertEqual((work, len(problems)), ("", 1))

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

    def work_after(self, record, plan_text):
        """next_work against the commit origin holds now, after `record` was merged."""
        return planrecord.next_work(str(self.repo), self.tip_of(), record, plan_text)


class Guards(Records):
    def test_a_plan_name_that_is_not_a_plan_name_is_a_named_problem(self):
        for name in ("..", "a/../b", "../x", "", "-x", "a b"):
            with self.subTest(name=name):
                self.assertEqual(len(planrecord.plan_problems(str(self.repo), self.tip_of(), name)), 1)

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

        problems = planrecord.plan_problems(str(self.repo), self.tip_of(), "0004-nomd")

        self.assertEqual(len(problems), 1)
        self.assertIn("md", problems[0])

    def test_a_plan_with_both_files_has_no_problem(self):
        self.assertEqual(planrecord.plan_problems(str(self.repo), self.tip_of(), PLAN), ())

    def test_a_plan_without_its_declaration_is_a_named_problem(self):
        self.merge_branch("half", {f"{planscope.AUTHORISED}/0003-half.md": "# p\n"})
        self.publish()

        self.assertEqual(len(planrecord.plan_problems(str(self.repo), self.tip_of(), "0003-half")), 1)

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

    def test_a_current_fetch_names_the_default_branch_and_the_commit_it_holds(self):
        self.assertEqual(self.fetched(), planrecord.Fetched(DEFAULT, self.tip_of()))

    def test_a_record_is_read_from_the_checked_commit_not_a_moving_ref(self):
        """A fetch landing between the check and the read must not change the answer."""
        tip = self.tip_of()
        self.merge_record(1, 1)  # moves refs/remotes/origin/<default> past the checked commit

        self.assertIsNone(planrecord.latest(str(self.repo), tip, PLAN).path)
        self.assertEqual(planrecord.latest(str(self.repo), self.tip_of(), PLAN).item, 1)

    def test_an_unvalidated_repository_is_refused_before_any_read(self):
        gh = FakeGh(self.answers)

        with self.assertRaises(PlanRecordError):
            planrecord.fetched(str(self.repo), "owner/name/../../other/repo", run=gh)

        self.assertEqual(gh.calls, [])

    def test_each_planauth_wrapper_refuses_an_unvalidated_repository_on_its_own(self):
        """Either wrapper may be called directly, so each guards its own REST path."""
        for call in (lambda api: planauth.default_branch(api, "owner/name/../../other/repo"),
                     lambda api: planauth.fetched_tip(str(self.repo), api, "owner/name/../../other", DEFAULT)):
            with self.subTest(call=call):
                gh = FakeGh(self.answers)

                with self.assertRaisesRegex(planauth.PlanAuthError, "is not owner/name"):
                    call(GhApi(gh))

                self.assertEqual(gh.calls, [])

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

    def test_a_malformed_plan_name_exits_1_even_when_the_fetch_is_stale(self):
        """The name is checked before any read: no fetch fixes a typo, so it must not read as exit 2."""
        self.answers[tpa.BRANCH_PATH] = json.dumps({"name": DEFAULT, "commit": {"sha": "f" * 40}})

        done = self.cli(command=("latest-record",), plan="../etc/passwd")

        self.assertEqual((done.returncode, done.stdout, "is not a plan name" in done.stderr), (1, "", True),
                         done.stderr)

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

    def test_next_on_restart_for_a_plan_with_no_items_section_exits_1(self):
        plan = "0005-noitems"
        merge = self.merge_branch("noitems", {
            f"{planscope.AUTHORISED}/{plan}.md": "# plan\n\n1. first\n2. second\n",
            f"{planscope.AUTHORISED}/{plan}.writes": "",
            f"{planscope.PLANS_DIR}/progress/{plan}/item-01-part1.md": record_text("item 1 complete")})
        self.publish()
        self.answers[tpa.pulls_path(merge)] = json.dumps([tpa.listed_pull(tpa.LATER_PR)])
        self.answers[tpa.pull_path(tpa.LATER_PR)] = tpa.pull_row(tpa.LATER_PR, merge)

        done = self.cli("--next-on-restart", command=("latest-record",), plan=plan)

        self.assertEqual((done.returncode, done.stdout, "## Items" in done.stderr), (1, "", True), done.stderr)

    def test_next_on_restart_with_an_unparseable_status_exits_1(self):
        self.merge_branch("rec", {record_path(1, 1): record_text("nonsense")})
        self.publish()
        self.answers[tpa.pulls_path(self.plan2_merge)] = json.dumps([tpa.listed_pull(tpa.LATER_PR)])
        self.answers[tpa.pull_path(tpa.LATER_PR)] = tpa.pull_row(tpa.LATER_PR, self.plan2_merge)

        done = self.run_cli("--next-on-restart")

        self.assertEqual((done.returncode, done.stdout), (1, ""), done.stderr)



if __name__ == "__main__":
    unittest.main()
