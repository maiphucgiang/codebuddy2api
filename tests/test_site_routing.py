"""Offline routing tests using synthetic unsigned JWTs and no user credentials."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 仓库根：允许直接运行本文件

import base64
import copy
import json
import unittest

from app.site_routing import (
    DOMESTIC,
    DOMESTIC_ENDPOINT,
    INTERNATIONAL,
    INTERNATIONAL_ENDPOINT,
    chat_url_for_headers,
    normalize_domain,
    refresh_url_for_auth,
    site_for_auth,
    site_for_headers,
)


HOSTS = {
    "www.codebuddy.cn": DOMESTIC,
    "www.workbuddy.cn": DOMESTIC,
    "copilot.tencent.com": DOMESTIC,
    "www.codebuddy.ai": INTERNATIONAL,
    "www.workbuddy.ai": INTERNATIONAL,
}
ENDPOINTS = {host: (DOMESTIC_ENDPOINT if host in ("www.codebuddy.cn", "copilot.tencent.com")
                   else "https://" + host) for host in HOSTS}
BAD_HINTS = (
    "unknown.invalid",
    "https://unknown.invalid/auth/realms/example",
    "http://www.codebuddy.ai",
    "ftp://www.codebuddy.ai",
    "//www.codebuddy.ai",
    "https://user@www.codebuddy.ai",
    "https://user:password@www.codebuddy.ai",
    "https://www.codebuddy.ai@unknown.invalid",
    "https://www.codebuddy.ai:443",
    "https://www.codebuddy.ai:",
    "www.codebuddy.ai:8080",
    "https://www.codebuddy.ai.unknown.invalid",
    "https://www.codebuddy.ai.",
    "https://www.codebuddy.ai?target=unknown.invalid",
    "https://www.codebuddy.ai#fragment",
    "https://www.codebuddy.ai?",
    "https://www.codebuddy.ai#",
    "https://www.codebuddy.ai\\@unknown.invalid",
    "https://www.codebuddy.ai\n",
    "https://www.codebuddy.ai\r\nX-Other: bad",
    "https://www.code\tbuddy.ai",
    "https://www.codebuddy.ai\x00",
    "https://www.codebuddy.ai\x7f",
    "https://[invalid",
    "https://%77ww.codebuddy.ai",
    "https://ｗｗｗ.codebuddy.ai",
    " ",
    42,
    False,
    [],
    {},
)


def jwt(claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "eyJhbGciOiJub25lIn0." + payload + ".synthetic-signature"


class SiteRoutingTests(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(DOMESTIC, "domestic")
        self.assertEqual(INTERNATIONAL, "international")
        self.assertEqual(DOMESTIC_ENDPOINT, "https://copilot.tencent.com")
        self.assertEqual(INTERNATIONAL_ENDPOINT, "https://www.codebuddy.ai")

    def test_domains_and_normalization(self):
        for host, site in HOSTS.items():
            for value in (host, host.upper(), "https://" + host, "HTTPS://" + host.upper() + "/", " " + host + " "):
                with self.subTest(value=value):
                    self.assertEqual(normalize_domain(value), host)
                    self.assertEqual(site_for_auth({"domain": value}), site)

    def test_all_issuers_with_realm_paths(self):
        for host, site in HOSTS.items():
            for issuer in (host, "https://" + host, "HTTPS://" + host.upper() + "/auth/realms/synthetic"):
                with self.subTest(issuer=issuer):
                    self.assertEqual(site_for_auth({"accessToken": jwt({"iss": issuer})}), site)

    def test_aliases_agree_within_site(self):
        for domain, domain_site in HOSTS.items():
            for issuer, issuer_site in HOSTS.items():
                if domain_site != issuer_site:
                    continue
                with self.subTest(domain=domain, issuer=issuer):
                    auth = {"domain": domain, "accessToken": jwt({"iss": "https://" + issuer + "/auth/realms/example"})}
                    self.assertEqual(site_for_auth(auth), domain_site)

    def test_legacy_default(self):
        for auth in (
            {}, {"domain": None}, {"domain": ""},
            {"accessToken": "old-synthetic-token"},
            {"accessToken": "header.%%%invalid%%%.signature"},
            {"accessToken": "header._w.signature"},
            {"accessToken": "header.bm90LWpzb24.signature"},
            {"accessToken": jwt({})},
            {"accessToken": jwt({"sub": "synthetic"})},
            {"accessToken": jwt([])},
            {"accessToken": jwt({"iss": None})},
            {"accessToken": jwt({"iss": ""})},
        ):
            with self.subTest(auth=auth):
                self.assertEqual(site_for_auth(auth), DOMESTIC)
                self.assertEqual(refresh_url_for_auth(auth), DOMESTIC_ENDPOINT + "/v2/plugin/auth/token/refresh")

    def test_explicit_domain_survives_unreadable_token(self):
        self.assertEqual(site_for_auth({"domain": "www.workbuddy.ai", "accessToken": "synthetic"}), INTERNATIONAL)

    def test_cross_site_conflicts(self):
        for domain, domain_site in HOSTS.items():
            for issuer, issuer_site in HOSTS.items():
                if domain_site == issuer_site:
                    continue
                auth = {"domain": domain, "accessToken": jwt({"iss": "https://" + issuer})}
                headers = {"X-Domain": domain, "Authorization": "Bearer " + auth["accessToken"]}
                for func, value in ((site_for_auth, auth), (refresh_url_for_auth, auth), (site_for_headers, headers), (chat_url_for_headers, headers)):
                    with self.subTest(domain=domain, issuer=issuer, api=func.__name__):
                        with self.assertRaises(ValueError):
                            func(value)

    def test_unknown_or_malicious_domain_rejected_even_with_known_issuer(self):
        for value in BAD_HINTS:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    normalize_domain(value)
                with self.assertRaises(ValueError):
                    site_for_auth({"domain": value})
                with self.assertRaises(ValueError):
                    site_for_auth({"domain": value, "accessToken": jwt({"iss": "https://www.codebuddy.cn"})})

    def test_unknown_or_malicious_issuer_rejected_even_with_known_domain(self):
        for value in BAD_HINTS:
            for domain in (None, "copilot.tencent.com", "www.workbuddy.ai"):
                with self.subTest(value=value, domain=domain):
                    with self.assertRaises(ValueError):
                        site_for_auth({"domain": domain, "accessToken": jwt({"iss": value})})

    def test_domain_origin_does_not_accept_issuer_path(self):
        value = "https://www.codebuddy.ai/auth/realms/example"
        with self.assertRaises(ValueError):
            normalize_domain(value)
        self.assertEqual(site_for_auth({"accessToken": jwt({"iss": value})}), INTERNATIONAL)

    def test_all_public_routes_are_consistent(self):
        for host, site in HOSTS.items():
            token = jwt({"iss": "https://" + host + "/auth/realms/example"})
            for domain in (None, host, "https://" + host + "/"):
                for bearer in ("Bearer", "bearer", "BEARER"):
                    auth = {"domain": domain, "accessToken": token}
                    headers = {"x-DoMaIn": domain, "aUtHoRiZaTiOn": bearer + " " + token}
                    with self.subTest(host=host, domain=domain, bearer=bearer):
                        self.assertEqual(site_for_auth(auth), site)
                        self.assertEqual(site_for_headers(headers), site)
                        self.assertEqual(chat_url_for_headers(headers), ENDPOINTS[host] + "/v2/chat/completions")
                        self.assertEqual(refresh_url_for_auth(auth), ENDPOINTS[host] + "/v2/plugin/auth/token/refresh")

    def test_header_only_and_legacy_default(self):
        for host, site in HOSTS.items():
            self.assertEqual(site_for_headers({"X-Domain": host}), site)
        for headers in ({}, {"Authorization": "Bearer synthetic"}, {"Authorization": "Basic synthetic"}):
            self.assertEqual(site_for_headers(headers), DOMESTIC)
            self.assertEqual(chat_url_for_headers(headers), DOMESTIC_ENDPOINT + "/v2/chat/completions")

    def test_duplicate_case_insensitive_headers(self):
        for headers in (
            {"X-Domain": "www.codebuddy.ai", "x-domain": "www.codebuddy.cn"},
            {"Authorization": "Bearer synthetic-one", "authorization": "Bearer synthetic-two"},
        ):
            with self.assertRaises(ValueError):
                site_for_headers(headers)
        self.assertEqual(site_for_headers({"X-Domain": "www.workbuddy.ai", "x-domain": "www.workbuddy.ai"}), INTERNATIONAL)

    def test_invalid_hints_cannot_become_targets(self):
        for value in BAD_HINTS:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    chat_url_for_headers({"X-Domain": value})
                with self.assertRaises(ValueError):
                    refresh_url_for_auth({"domain": value})

    def test_errors_do_not_include_untrusted_values(self):
        marker = "private-untrusted-marker.invalid"
        token = jwt({"iss": "https://" + marker})
        for func, value in (
            (normalize_domain, "https://" + marker),
            (site_for_auth, {"accessToken": token}),
            (refresh_url_for_auth, {"domain": marker}),
            (chat_url_for_headers, {"Authorization": "Bearer " + token}),
        ):
            with self.subTest(api=func.__name__):
                with self.assertRaises(ValueError) as caught:
                    func(value)
                self.assertNotIn(marker, str(caught.exception))
                self.assertNotIn(token, str(caught.exception))

    def test_inputs_are_not_mutated(self):
        auth = {"domain": "https://www.workbuddy.ai/", "accessToken": jwt({"iss": "https://www.workbuddy.ai"})}
        headers = {"X-Domain": auth["domain"], "Authorization": "Bearer " + auth["accessToken"]}
        saved = copy.deepcopy((auth, headers))
        site_for_auth(auth)
        refresh_url_for_auth(auth)
        site_for_headers(headers)
        chat_url_for_headers(headers)
        self.assertEqual((auth, headers), saved)


if __name__ == "__main__":
    unittest.main(verbosity=2)
