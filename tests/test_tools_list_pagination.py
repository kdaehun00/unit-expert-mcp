"""``toolsList.pageSize`` / ``nextCursor`` / ``failAtPage`` on both eras."""

from __future__ import annotations

import unittest

from mcp_test_support import (
    ERR_INVALID_PARAMS,
    HttpServerTestCase,
    legacy_request,
    modern_headers,
    modern_request,
)
from unit_expert_mcp.server import handle_json_rpc, inspect_snapshot, normalize_config

ERAS = ("legacy", "modern")


def list_tools(era: str, config: dict | None, cursor: object = None):
    params = {} if cursor is None else {"cursor": cursor}
    if era == "modern":
        return handle_json_rpc(
            modern_request("tools/list", **params), config=config, headers=modern_headers("tools/list")
        )
    return handle_json_rpc(legacy_request("tools/list", **params), config=config)


def walk(era: str, config: dict) -> tuple[list[list[str]], list[dict]]:
    """Follow nextCursor to the end. Returns (tool names per page, results)."""
    pages: list[list[str]] = []
    results: list[dict] = []
    cursor = None
    for _ in range(50):
        status, _, payload = list_tools(era, config, cursor)
        assert status == 200 and payload is not None and "result" in payload, payload
        result = payload["result"]
        results.append(result)
        pages.append([tool["name"] for tool in result["tools"]])
        cursor = result.get("nextCursor")
        if cursor is None:
            return pages, results
    raise AssertionError("pagination did not terminate")


def paged(size: int, **extra: object) -> dict:
    return {"toolsList": {"pageSize": size, **extra}}


class PaginationTest(unittest.TestCase):
    def test_default_has_no_pagination(self) -> None:
        self.assertEqual(normalize_config({})["toolsList"]["pageSize"], 0)
        for era in ERAS:
            with self.subTest(era=era):
                _, _, payload = list_tools(era, None)
                assert payload is not None
                self.assertNotIn("nextCursor", payload["result"])

    def test_pages_cover_every_tool_exactly_once_in_order(self) -> None:
        for era, total in (("legacy", 6), ("modern", 9)):
            _, _, everything = list_tools(era, None)
            assert everything is not None
            all_names = [tool["name"] for tool in everything["result"]["tools"]]
            self.assertEqual(len(all_names), total)
            for size in (1, 2, 3, 5, total, total + 1):
                with self.subTest(era=era, size=size):
                    pages, _ = walk(era, paged(size))
                    self.assertEqual([name for page in pages for name in page], all_names)
                    self.assertEqual(len(pages), -(-total // size))
                    self.assertTrue(all(len(page) <= size for page in pages))

    def test_last_page_has_no_next_cursor_and_others_have_a_string(self) -> None:
        for era in ERAS:
            with self.subTest(era=era):
                _, results = walk(era, paged(4))
                self.assertNotIn("nextCursor", results[-1])
                for result in results[:-1]:
                    self.assertIsInstance(result["nextCursor"], str)
                    self.assertTrue(result["nextCursor"])

    def test_modern_pages_keep_the_cache_fields_and_envelope(self) -> None:
        config = {"toolsList": {"pageSize": 3, "cacheTtlMs": 5000, "cacheScope": "public"}}
        _, results = walk("modern", config)
        self.assertGreater(len(results), 1)
        for result in results:
            self.assertEqual(result["resultType"], "complete")
            self.assertEqual(result["ttlMs"], 5000)
            self.assertEqual(result["cacheScope"], "public")

    def test_legacy_pages_have_no_modern_fields(self) -> None:
        _, results = walk("legacy", paged(4))
        for result in results:
            self.assertNotIn("resultType", result)
            self.assertNotIn("ttlMs", result)

    def test_invalid_cursor_is_invalid_params(self) -> None:
        _, results = walk("modern", paged(3))
        valid = results[0]["nextCursor"]
        for era in ERAS:
            for cursor in ("not-a-cursor", "", valid + "x", 7, "eyJvZmZzZXQiOjk5OX0", "eyJvZmZzZXQiOi0xfQ"):
                with self.subTest(era=era, cursor=cursor):
                    status, _, payload = list_tools(era, paged(3), cursor)
                    assert payload is not None
                    self.assertEqual(payload["error"]["code"], ERR_INVALID_PARAMS)
                    self.assertEqual(status, 200)

    def test_modern_invalid_cursor_status_follows_the_config(self) -> None:
        config = {**paged(3), "modern": {"invalidParamsHttpStatus": 400}}
        status, _, payload = list_tools("modern", config, "not-a-cursor")
        self.assertEqual(status, 400)
        assert payload is not None
        self.assertEqual(payload["error"]["code"], ERR_INVALID_PARAMS)

    def test_cursor_without_pagination(self) -> None:
        # Modern: this server issued no cursor, so any cursor is unknown.
        status, _, payload = list_tools("modern", None, "anything")
        assert payload is not None
        self.assertEqual(payload["error"]["code"], ERR_INVALID_PARAMS)
        # Legacy default behaviour is unchanged: the cursor is ignored.
        status, _, payload = list_tools("legacy", None, "anything")
        self.assertEqual(status, 200)
        assert payload is not None
        self.assertEqual(len(payload["result"]["tools"]), 6)

    def test_pagination_applies_to_generated_tool_lists(self) -> None:
        config = {"toolsList": {"mode": "too-many", "tooManyCount": 25, "pageSize": 10}}
        for era in ERAS:
            with self.subTest(era=era):
                pages, _ = walk(era, config)
                self.assertEqual([len(page) for page in pages], [10, 10, 5])

    def test_null_and_error_modes_are_not_paginated(self) -> None:
        for era in ERAS:
            with self.subTest(era=era):
                _, _, payload = list_tools(era, {"toolsList": {"mode": "null", "pageSize": 2}})
                assert payload is not None
                self.assertIsNone(payload["result"]["tools"])
                _, _, payload = list_tools(era, {"toolsList": {"mode": "json-rpc-error", "pageSize": 2}})
                assert payload is not None
                self.assertEqual(payload["error"]["code"], -32603)

    def test_fail_at_page_breaks_the_walk_midway(self) -> None:
        config = paged(2, failAtPage=2)
        for era in ERAS:
            with self.subTest(era=era):
                status, _, first = list_tools(era, config)
                self.assertEqual(status, 200)
                assert first is not None
                self.assertEqual(len(first["result"]["tools"]), 2)
                status, _, second = list_tools(era, config, first["result"]["nextCursor"])
                self.assertEqual(status, 200)
                assert second is not None
                self.assertEqual(second["error"]["code"], -32603)

    def test_fail_at_page_needs_pagination(self) -> None:
        _, _, payload = list_tools("legacy", {"toolsList": {"failAtPage": 1}})
        assert payload is not None
        self.assertIn("result", payload)

    def test_config_validation(self) -> None:
        for bad in (
            {"toolsList": {"pageSize": -1}},
            {"toolsList": {"pageSize": 101}},
            {"toolsList": {"pageSize": "2"}},
            {"toolsList": {"pageSize": True}},
            {"toolsList": {"failAtPage": -1}},
        ):
            with self.subTest(config=bad):
                with self.assertRaises(ValueError):
                    normalize_config(bad)


class PaginationHttpTest(HttpServerTestCase):
    def test_walk_over_http_on_both_eras(self) -> None:
        config = paged(4)
        for era, total in (("legacy", 6), ("modern", 9)):
            with self.subTest(era=era):
                names: list[str] = []
                cursor = None
                for _ in range(10):
                    params = {} if cursor is None else {"cursor": cursor}
                    if era == "modern":
                        status, _, payload, _ = self.post(
                            modern_request("tools/list", **params),
                            config=config,
                            headers=modern_headers("tools/list"),
                        )
                    else:
                        status, _, payload, _ = self.post(legacy_request("tools/list", **params), config=config)
                    self.assertEqual(status, 200)
                    names.extend(tool["name"] for tool in payload["result"]["tools"])
                    cursor = payload["result"].get("nextCursor")
                    if cursor is None:
                        break
                self.assertEqual(len(names), total)
                self.assertEqual(len(set(names)), total)

    def test_inspect_log_names_the_option(self) -> None:
        self.post(legacy_request("tools/list"), config=paged(2, failAtPage=1))
        self.assertEqual(
            inspect_snapshot()[0]["injected"], ["toolsList.pageSize=2", "toolsList.failAtPage=1"]
        )

    def test_via_header_scenario(self) -> None:
        status, _, payload, _ = self.post(
            legacy_request("tools/list"), headers={"X-MCP-Test-Scenario": "tools-list-paged"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["result"]["tools"]), 2)
        self.assertIn("nextCursor", payload["result"])


if __name__ == "__main__":
    unittest.main()
