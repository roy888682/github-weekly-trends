"""weekly_trends.py 단위 테스트 — 네트워크 없이 가짜 GitHub·Claude·SMTP 로 검증합니다.

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import weekly_trends as wt  # noqa: E402

NOW = datetime(2026, 12, 1, 18, 0, tzinfo=timezone.utc)  # = 2026-12-02(수) 03:00 KST (정기 실행 시각)


def setUpModule():
    # 테스트가 GitHub Actions 안에서 돌 때도 ::warning:: 주석이나 Job Summary 를 남기지 않도록 합니다.
    for patcher in (mock.patch.object(wt, "IN_ACTIONS", False),
                    mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": ""})):
        patcher.start()
        unittest.addModuleCleanup(patcher.stop)


@contextlib.contextmanager
def quiet():
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        yield


def make_repo(i: int, **overrides) -> dict:
    owner = overrides.pop("owner", f"owner{i}")
    name = overrides.pop("name", f"tool-{i}")
    repo = {
        "full_name": f"{owner}/{name}",
        "name": name,
        "html_url": f"https://github.com/{owner}/{name}",
        "description": f"A fast command line tool number {i} that helps developers automate boring tasks.",
        "language": "Python",
        "stargazers_count": 5000 - i * 10,
        "forks_count": 120 - i,
        "topics": ["cli", "automation"],
        "created_at": "2026-11-25T10:00:00Z",
        "homepage": "",
        "license": {"spdx_id": "MIT"},
        "fork": False,
        "archived": False,
        "disabled": False,
    }
    repo.update(overrides)
    return repo


SAMPLE_README = """<p align="center"><img src="logo.png"></p>

# Tool

[![CI](https://img.shields.io/badge/ci-passing-green)](https://example.com) [![PyPI](https://img.shields.io/pypi/v/tool)](https://pypi.org)

> Tool is a blazing fast task runner that turns plain YAML files into reproducible developer workflows.

## Install

```bash
pip install tool
```

- feature one
- feature two
"""

KOREAN_39 = ("Tool은 YAML 파일로 정의한 작업을 재현 가능한 개발 워크플로로 바꿔 주는 빠른 태스크 러너입니다. "
             "의존성 캐시와 병렬 실행을 기본으로 지원해 반복 작업을 크게 줄여 줍니다. "
             "로컬과 CI에서 같은 명령을 그대로 쓰고 싶은 개발자와 소규모 팀에게 특히 유용하며 설정도 간단합니다.")


def make_entry(rank: int, **overrides) -> wt.Entry:
    fields = dict(
        rank=rank, name=f"tool-{rank}", full_name=f"owner{rank}/tool-{rank}",
        url=f"https://github.com/owner{rank}/tool-{rank}",
        intro=wt.template_intro(make_repo(rank)), intro_source="template",
        stars=1000 - rank, forks=10, language="Python",
        created_at=NOW - timedelta(days=2), topics=["cli"], homepage="",
    )
    fields.update(overrides)
    return wt.Entry(**fields)


def fake_fetch_factory(repos: list[dict], readme: str = SAMPLE_README):
    calls: list[str] = []

    def fake_fetch(path, params=None, token="", *, accept=None, raw=False, retries=3):
        calls.append(path)
        if path == "/search/repositories":
            page = params["page"]
            return {"total_count": len(repos), "incomplete_results": False,
                    "items": repos[(page - 1) * 100: page * 100]}
        if path.endswith("/readme"):
            return readme
        raise AssertionError(f"예상하지 못한 API 경로: {path}")

    fake_fetch.calls = calls
    return fake_fetch


def fake_anthropic_module() -> types.ModuleType:
    """anthropic SDK 의 예외 계층만 흉내 낸 가짜 모듈(테스트 환경에 SDK 가 없어도 동작)."""
    module = types.ModuleType("anthropic")

    class APIError(Exception):
        def __init__(self, message="error"):
            super().__init__(message)
            self.message = message

    class APIStatusError(APIError):
        status_code = 500

    class APIConnectionError(APIError):
        pass

    class BadRequestError(APIStatusError):
        status_code = 400

    class AuthenticationError(APIStatusError):
        status_code = 401

    class PermissionDeniedError(APIStatusError):
        status_code = 403

    class NotFoundError(APIStatusError):
        status_code = 404

    class RateLimitError(APIStatusError):
        status_code = 429

    for cls in (APIError, APIStatusError, APIConnectionError, BadRequestError, AuthenticationError,
                PermissionDeniedError, NotFoundError, RateLimitError):
        setattr(module, cls.__name__, cls)
    module.Anthropic = mock.Mock(side_effect=AssertionError("테스트에서는 client 를 주입해야 합니다"))
    return module


class FakeClaudeClient:
    def __init__(self, *responses):
        self.calls: list[dict] = []
        self._responses = list(responses)
        self.beta = types.SimpleNamespace(messages=types.SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def claude_response(text: str, stop_reason: str = "end_turn"):
    return types.SimpleNamespace(
        stop_reason=stop_reason,
        content=[types.SimpleNamespace(type="thinking", thinking=""),
                 types.SimpleNamespace(type="text", text=text)],
    )


class WordCountTests(unittest.TestCase):
    def test_english_words(self):
        self.assertEqual(wt.count_words("This is a simple test."), 5)

    def test_korean_counts_eojeol(self):
        self.assertEqual(wt.count_words("한국어 문장은 띄어쓰기 기준으로 셉니다."), 5)

    def test_symbols_are_not_words(self):
        self.assertEqual(wt.count_words("Hello — world ★ · !"), 2)

    def test_cjk_without_spaces_counts_two_chars_per_word(self):
        self.assertEqual(wt.count_words("支持多种工具调用"), 4)

    def test_truncate_respects_limit_and_adds_ellipsis(self):
        cut = wt.truncate_words(" ".join(f"word{i}" for i in range(80)), 20)
        self.assertLessEqual(wt.count_words(cut), 20)
        self.assertTrue(cut.endswith("…"))

    def test_truncate_keeps_short_text(self):
        self.assertEqual(wt.truncate_words("short text", 10), "short text")

    def test_truncate_cjk(self):
        cut = wt.truncate_words("这是一个非常长的中文描述没有任何空格但是需要被截断到合适的长度", 6)
        self.assertLessEqual(wt.count_words(cut), 6)
        self.assertTrue(cut.endswith("…"))

    def test_sample_korean_summary_is_in_range(self):
        self.assertEqual(wt.count_words(KOREAN_39), 39)


class TemplateIntroTests(unittest.TestCase):
    CASES = {
        "normal": (make_repo(1), SAMPLE_README),
        "long_description": (make_repo(2, description=" ".join(["very"] * 90) + " long."), ""),
        "no_description_with_readme": (make_repo(3, description=None), SAMPLE_README),
        "no_description_no_readme": (make_repo(4, description="", topics=[], license=None), ""),
        "chinese_description": (make_repo(5, description="一个基于大模型的智能体框架，支持多种工具调用与长期记忆，开箱即用" * 3), ""),
        "noisy_description": (make_repo(6, description='Try https://example.com :rocket: **Fast** `cli` "quoted" tool'), ""),
        "no_language": (make_repo(7, language=None, topics=[], license={"spdx_id": "NOASSERTION"}), ""),
        "huge_numbers": (make_repo(8, stargazers_count=1234567, forks_count=98765), ""),
        "korean_description": (make_repo(9, description="한국어로 작성된 짧은 설명입니다"), ""),
    }

    def test_every_case_is_30_to_50_words(self):
        for label, (repo, readme_md) in self.CASES.items():
            with self.subTest(label):
                intro = wt.template_intro(repo, wt.readme_to_text(readme_md))
                words = wt.count_words(intro)
                self.assertGreaterEqual(words, wt.MIN_WORDS, intro)
                self.assertLessEqual(words, wt.MAX_WORDS, intro)
                self.assertNotIn("http", intro)
                self.assertNotIn('""', intro)

    def test_uses_readme_when_description_is_missing(self):
        intro = wt.template_intro(make_repo(3, description=None), wt.readme_to_text(SAMPLE_README))
        self.assertIn("blazing fast task runner", intro)

    def test_is_deterministic(self):
        repo = make_repo(1)
        self.assertEqual(wt.template_intro(repo, SAMPLE_README), wt.template_intro(repo, SAMPLE_README))


class ReadmeTests(unittest.TestCase):
    def test_strips_badges_html_and_code(self):
        text = wt.readme_to_text(SAMPLE_README)
        self.assertNotIn("img.shields.io", text)
        self.assertNotIn("pip install", text)
        self.assertNotIn("<p", text)

    def test_lead_paragraph_skips_headings_badges_and_lists(self):
        lead = wt.lead_paragraph(wt.readme_to_text(SAMPLE_README))
        self.assertTrue(lead.startswith("Tool is a blazing fast task runner"), lead)

    def test_safe_homepage(self):
        repo_url = "https://github.com/a/b"
        self.assertEqual(wt.safe_homepage("example.com", repo_url), "https://example.com")
        self.assertEqual(wt.safe_homepage("https://github.com/a/b/", repo_url), "")
        self.assertEqual(wt.safe_homepage("javascript:alert(1)", repo_url), "")
        self.assertEqual(wt.safe_homepage(None, repo_url), "")


class SelectionTests(unittest.TestCase):
    def test_filters_non_programs_and_keeps_star_order(self):
        repos = [
            make_repo(0, fork=True),
            make_repo(1, archived=True),
            make_repo(2, language=None),
            make_repo(3, name="awesome-agents", description="A curated list of agent resources."),
            make_repo(4, owner="bigcorp", name="a"),
            make_repo(5, owner="bigcorp", name="b"),
            make_repo(6, owner="bigcorp", name="c"),   # 같은 소유자 세 번째 → 제외
            make_repo(7, description=""),              # 설명도 README 도 없음 → 제외
        ] + [make_repo(i) for i in range(10, 30)]
        readmes = {r["full_name"]: SAMPLE_README for r in repos}
        readmes["owner7/tool-7"] = ""
        with quiet():
            entries = wt.select_entries(repos, wt.Config(max_per_owner=2), lambda n: readmes.get(n, ""))
        self.assertEqual(len(entries), wt.TOP_N)
        names = [e.full_name for e in entries]
        for excluded in ("owner0/tool-0", "owner1/tool-1", "owner2/tool-2", "owner3/awesome-agents",
                         "bigcorp/c", "owner7/tool-7"):
            self.assertNotIn(excluded, names)
        self.assertEqual(sum(n.startswith("bigcorp/") for n in names), 2)
        self.assertEqual([e.rank for e in entries], list(range(1, 11)))
        self.assertEqual([e.stars for e in entries], sorted((e.stars for e in entries), reverse=True))
        wt.validate_entries(entries)

    def test_exclude_keywords(self):
        cfg = wt.Config(exclude_keywords=("proxy",))
        self.assertIsNotNone(wt.exclusion_reason(make_repo(1, description="Free proxy node list"), cfg))
        self.assertIsNone(wt.exclusion_reason(make_repo(2), cfg))

    def test_awesome_in_description_is_not_a_curated_list(self):
        self.assertIsNone(wt.exclusion_reason(make_repo(1, description="An awesome CLI for logs"), wt.Config()))

    def test_fewer_than_ten_candidates_fail_validation(self):
        with quiet():
            entries = wt.select_entries([make_repo(i) for i in range(5)], wt.Config(), lambda n: SAMPLE_README)
        self.assertEqual(len(entries), 5)
        with self.assertRaises(wt.DigestError):
            wt.validate_entries(entries)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.entries = [make_entry(i) for i in range(1, 11)]

    def test_valid_digest_passes(self):
        wt.validate_entries(self.entries)

    def test_nine_entries_fail(self):
        with self.assertRaises(wt.DigestError):
            wt.validate_entries(self.entries[:9])

    def test_short_intro_fails(self):
        self.entries[3].intro = "너무 짧은 소개입니다."
        with self.assertRaises(wt.DigestError):
            wt.validate_entries(self.entries)

    def test_long_intro_fails(self):
        self.entries[0].intro = " ".join(["단어"] * 51)
        with self.assertRaises(wt.DigestError):
            wt.validate_entries(self.entries)

    def test_missing_link_fails(self):
        self.entries[5].url = ""
        with self.assertRaises(wt.DigestError):
            wt.validate_entries(self.entries)

    def test_duplicate_repository_fails(self):
        self.entries[9] = make_entry(10, full_name=self.entries[0].full_name)
        with self.assertRaises(wt.DigestError):
            wt.validate_entries(self.entries)


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.entries = [make_entry(i) for i in range(1, 11)]
        self.entries[0].name = "evil_<script>*tool*"
        self.entries[0].intro = wt.template_intro(
            make_repo(1, description="Uses <script>alert(1)</script> and *stars* | pipes"))
        self.entries[1].homepage = "https://example.com/docs"
        self.since, self.until = NOW - timedelta(days=7), NOW

    def test_markdown_has_ten_items_with_links_and_escapes_input(self):
        md = wt.render_markdown(self.entries, self.since, self.until, NOW, "테스트")
        wt.validate_markdown(md)
        self.assertEqual(len(re.findall(r"^## \d+\. ", md, flags=re.M)), 10)
        self.assertEqual(len(re.findall(r"^- 공식 링크: <https://github\.com/", md, flags=re.M)), 10)
        self.assertIn("- 홈페이지: <https://example.com/docs>", md)
        self.assertIn("2026-11-25(수) 03:00 ~ 2026-12-02(수) 03:00 (KST)", md)
        self.assertNotIn("<script>", md)
        self.assertIn("evil\\_\\<script\\>\\*tool\\*", md)

    def test_html_escapes_user_text(self):
        page = wt.render_html(self.entries, self.since, self.until, NOW, "테스트")
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertEqual(page.count("공식 링크:"), 10)

    def test_subject(self):
        self.assertEqual(wt.build_subject(NOW), "[GitHub 주간 트렌드] 2026-12-02 신규 인기 프로그램 TOP 10")


class ClaudeSummarizerTests(unittest.TestCase):
    def setUp(self):
        self.fake = fake_anthropic_module()
        patcher = mock.patch.dict(sys.modules, {"anthropic": self.fake})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = make_repo(1)

    def make(self, *responses):
        client = FakeClaudeClient(*responses)
        return wt.ClaudeSummarizer("test-key", client=client), client

    def test_valid_summary_is_used_and_request_is_well_formed(self):
        summarizer, client = self.make(claude_response(KOREAN_39))
        self.assertEqual(summarizer.summarize(self.repo, "readme text"), KOREAN_39)
        call = client.calls[0]
        self.assertEqual(call["model"], "claude-opus-5-5")
        self.assertEqual(call["fallbacks"], "default")
        self.assertIn("server-side-fallback-2026-07-01", call["betas"])
        self.assertEqual(call["output_config"], {"effort": "medium"})
        self.assertIn("<repository>", call["messages"][0]["content"])
        self.assertIn("readme text", call["messages"][0]["content"])

    def test_retries_once_when_length_is_off(self):
        summarizer, client = self.make(claude_response("짧은 소개입니다."), claude_response(KOREAN_39))
        with quiet():
            self.assertEqual(summarizer.summarize(self.repo, ""), KOREAN_39)
        self.assertEqual(len(client.calls), 2)
        self.assertIn("직전 시도", client.calls[1]["messages"][0]["content"])

    def test_gives_up_after_two_bad_lengths(self):
        summarizer, _ = self.make(claude_response("짧음."), claude_response("또 짧음."))
        with quiet():
            self.assertIsNone(summarizer.summarize(self.repo, ""))

    def test_refusal_falls_back_to_template(self):
        summarizer, _ = self.make(claude_response("", stop_reason="refusal"))
        with quiet():
            self.assertIsNone(summarizer.summarize(self.repo, ""))

    def test_auth_error_disables_further_calls(self):
        summarizer, client = self.make(self.fake.AuthenticationError("invalid x-api-key"))
        with quiet():
            self.assertIsNone(summarizer.summarize(self.repo, ""))
            self.assertIsNone(summarizer.summarize(make_repo(2), ""))
        self.assertFalse(summarizer.enabled)
        self.assertEqual(len(client.calls), 1)

    def test_rate_limit_falls_back_without_disabling(self):
        summarizer, _ = self.make(self.fake.RateLimitError("slow down"), claude_response(KOREAN_39))
        with quiet():
            self.assertIsNone(summarizer.summarize(self.repo, ""))
        self.assertTrue(summarizer.enabled)
        self.assertEqual(summarizer.summarize(make_repo(2), ""), KOREAN_39)

    def test_strips_urls_and_wrapping_quotes(self):
        summarizer, _ = self.make(claude_response(f'"{KOREAN_39} https://example.com"'))
        out = summarizer.summarize(self.repo, "")
        self.assertEqual(out, KOREAN_39)

    def test_long_readme_is_capped_and_noted(self):
        with quiet():
            prompt = wt.build_summary_prompt(self.repo, "x" * (wt.README_PROMPT_CHARS + 500))
        self.assertIn("중 앞부분", prompt)
        self.assertLess(len(prompt), wt.README_PROMPT_CHARS + 2000)

    def test_selection_uses_template_when_claude_fails(self):
        summarizer, _ = self.make(*[claude_response("짧음.") for _ in range(40)])
        with quiet():
            entries = wt.select_entries([make_repo(i) for i in range(12)], wt.Config(),
                                        lambda n: SAMPLE_README, summarizer)
        self.assertEqual(len(entries), 10)
        self.assertTrue(all(e.intro_source == "template" for e in entries))
        wt.validate_entries(entries)


class SearchTests(unittest.TestCase):
    def test_query_covers_last_seven_days_in_utc(self):
        self.assertEqual(wt.build_search_query(NOW - timedelta(days=7), NOW),
                         "created:2026-11-24T18:00:00+00:00..2026-12-01T18:00:00+00:00 is:public archived:false")

    def test_iter_candidates_requests_next_page_only_when_needed(self):
        fetch = fake_fetch_factory([make_repo(i) for i in range(150)])
        with quiet():
            items = list(wt.iter_candidates(NOW - timedelta(days=7), NOW, fetch=fetch))
        self.assertEqual(len(items), 150)
        self.assertEqual(fetch.calls.count("/search/repositories"), 2)


class PipelineTests(unittest.TestCase):
    def test_run_sends_one_email_with_exactly_ten_items(self):
        fetch = fake_fetch_factory([make_repo(i) for i in range(40)])
        sent = []
        with tempfile.TemporaryDirectory() as tmp, quiet():
            cfg = wt.Config(mail_from="sender@example.com", mail_to=["team@example.com"],
                            smtp_password="x", output_dir=Path(tmp))
            md_path = wt.run(cfg, now=NOW, fetch=fetch, sender=lambda *args: sent.append(args))
            self.assertEqual(md_path.name, "2026-12-02.md")
            self.assertTrue((Path(tmp) / "2026-12-02.html").exists())
        self.assertEqual(len(sent), 1)
        _, subject, text_body, html_body = sent[0]
        self.assertIn("TOP 10", subject)
        self.assertEqual(len(re.findall(r"^## \d+\. ", text_body, flags=re.M)), 10)
        self.assertEqual(html_body.count("공식 링크:"), 10)
        self.assertEqual(fetch.calls.count("/search/repositories"), 1)

    def test_dry_run_does_not_send(self):
        fetch = fake_fetch_factory([make_repo(i) for i in range(40)])
        with tempfile.TemporaryDirectory() as tmp, quiet():
            cfg = wt.Config(dry_run=True, output_dir=Path(tmp))
            wt.run(cfg, now=NOW, fetch=fetch, sender=lambda *args: self.fail("dry run 에서 메일이 발송됨"))


class ConfigTests(unittest.TestCase):
    def test_from_env_parses_values_and_strips_app_password(self):
        env = {"MAIL_FROM": "sender@example.com", "MAIL_TO": "a@example.com, b@example.com",
               "SMTP_PASSWORD": "abcd efgh ijkl mnop", "ANTHROPIC_API_KEY": "",
               "MAX_PER_OWNER": "3", "EXCLUDE_KEYWORDS": "Proxy, VPN"}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = wt.Config.from_env()
        self.assertEqual(cfg.mail_to, ["a@example.com", "b@example.com"])
        self.assertEqual(cfg.smtp_password, "abcdefghijklmnop")
        self.assertEqual(cfg.anthropic_api_key, "")
        self.assertEqual(cfg.max_per_owner, 3)
        self.assertEqual(cfg.exclude_keywords, ("proxy", "vpn"))
        cfg.check()

    def test_check_reports_missing_secrets(self):
        with self.assertRaises(wt.ConfigError) as ctx:
            wt.Config().check()
        self.assertIn("MAIL_FROM", str(ctx.exception))

    def test_check_rejects_malformed_address(self):
        with self.assertRaises(wt.ConfigError):
            wt.Config(mail_from="sender@example.com", mail_to=["not-an-email"], smtp_password="x").check()

    def test_dry_run_skips_secret_check(self):
        wt.Config(dry_run=True).check()

    def test_load_dotenv_does_not_override_existing_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("# comment\nMAIL_TO=\"x@example.com\"\nexport SMTP_HOST=smtp.example.com\n"
                            "MAIL_FROM=file@example.com\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"MAIL_FROM": "env@example.com"}, clear=True):
                wt.load_dotenv(path)
                self.assertEqual(os.environ["MAIL_TO"], "x@example.com")
                self.assertEqual(os.environ["SMTP_HOST"], "smtp.example.com")
                self.assertEqual(os.environ["MAIL_FROM"], "env@example.com")


class EmailTests(unittest.TestCase):
    def test_send_email_builds_text_and_html_parts(self):
        cfg = wt.Config(mail_from="sender@example.com", mail_to=["a@example.com", "b@example.com"],
                        smtp_password="secretpw")
        with mock.patch.object(wt.smtplib, "SMTP_SSL") as smtp_cls:
            wt.send_email(cfg, "제목", "# 본문", "<p>본문</p>")
        server = smtp_cls.return_value
        server.login.assert_called_once_with("sender@example.com", "secretpw")
        msg = server.send_message.call_args.args[0]
        self.assertEqual(msg["To"], "a@example.com, b@example.com")
        self.assertEqual(msg["Subject"], "제목")
        self.assertIn("# 본문", msg.get_body(("plain",)).get_content())
        self.assertIn("<p>본문</p>", msg.get_body(("html",)).get_content())


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / ".github" / "workflows" / "weekly-trends.yml").read_text(encoding="utf-8")

    def test_runs_every_wednesday_at_3am_kst(self):
        self.assertRegex(self.text, r"(?m)^\s*- cron: '0 3 \* \* 3'")
        self.assertRegex(self.text, r"(?m)^\s*timezone: 'Asia/Seoul'")

    def test_email_footer_label_matches_cron(self):
        _, hour, _, _, weekday = re.search(r"cron: '([^']+)'", self.text).group(1).split()
        self.assertIn(f"{'일월화수목금토'[int(weekday)]}요일", wt.SCHEDULE_LABEL)
        self.assertIn(f"{int(hour) % 12 or 12}시", wt.SCHEDULE_LABEL)

    def test_manual_trigger_and_required_secrets(self):
        self.assertIn("workflow_dispatch:", self.text)
        for secret in ("MAIL_FROM", "MAIL_TO", "GMAIL_APP_PASSWORD"):
            self.assertIn(f"secrets.{secret}", self.text)


if __name__ == "__main__":
    unittest.main()
