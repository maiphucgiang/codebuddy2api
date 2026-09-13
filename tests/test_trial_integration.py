"""自动 Trial 开关、身份和维护流程回归；所有网络均为 Mock。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 仓库根：允许直接运行本文件

from copy import deepcopy
import json
import tempfile
import time
import unittest
from unittest.mock import patch

import converter
from app import credits
from app import trial_rewards


class TrialIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(converter.CONFIG, {
            "cred": None, "cred_pool": None, "ledger": None, "log_path": None,
            "auto_trial": False, "trial_ledger": trial_rewards.TrialLedger(self.root / "trials.json"),
        }))
        self.enterContext(patch.object(converter, "_log"))
        self.order = []
        self.claim = self.enterContext(patch.object(trial_rewards, "claim_trial", side_effect=self.claimed))
        self.balance = self.enterContext(patch.object(credits, "fetch_credits", side_effect=self.credited))
        self.checkin = self.enterContext(patch.object(credits, "daily_checkin"))

    def claimed(self, headers):
        self.order.append("trial")
        self.assertEqual(headers["X-Domain"], "www.workbuddy.ai")
        return {"ok": True, "already": False, "code": 0, "status": 200}

    def credited(self, token, **kwargs):
        self.order.append("credits")
        return {"credits": 123, "count": 1, "segments": [], "intl": self.domain.endswith(".ai")}

    def configure(self, domain="www.workbuddy.ai"):
        self.domain = domain
        self.path = self.root / "account.info"
        self.data = {"account": {"uid": "synthetic-user", "enterpriseId": "synthetic-tenant"},
                     "auth": {"domain": domain, "accessToken": "synthetic-token",
                              "refreshToken": "synthetic-refresh", "expiresAt": int((time.time() + 864000) * 1000)}}
        self.path.write_text(json.dumps(self.data))
        self.pool = converter.CredentialPool([self.path])
        self.ledger = credits.CreditLedger(self.root / "credits.json")
        self.pool.set_ledger(self.ledger)
        converter.CONFIG.update(cred_pool=self.pool, ledger=self.ledger)

    def sync(self):
        failed = set()
        result = converter._sync_credits(self.pool, self.ledger, self.pool.entries()[0], checkin=False, failed=failed)
        self.assertFalse(failed)
        self.assertIsNotNone(result)
        self.assertEqual(self.ledger.entry(str(self.path))["credits"]["credits"], 123)

    def test_default_off_still_refreshes_balance(self):
        self.configure()
        self.sync()
        self.claim.assert_not_called()
        self.assertEqual(self.order, ["credits"])

    def test_only_international_workbuddy_may_claim(self):
        converter.CONFIG["auto_trial"] = True
        for domain in ("www.codebuddy.cn", "www.workbuddy.cn", "www.codebuddy.ai"):
            with self.subTest(domain=domain):
                self.configure(domain)
                self.sync()
        self.claim.assert_not_called()

    def test_enabled_claims_once_before_authoritative_balance_query(self):
        self.configure()
        converter.CONFIG["auto_trial"] = True
        self.sync()
        self.sync()
        self.assertEqual(self.order, ["trial", "credits", "credits"])
        self.checkin.assert_not_called()

    def test_relogin_and_new_ledger_object_do_not_claim_twice(self):
        self.configure()
        converter.CONFIG["auto_trial"] = True
        self.sync()
        replacement = deepcopy(self.data)
        replacement["auth"]["accessToken"] = "replacement-synthetic-token"
        converter._store_credential(self.root, self.path.name, json.dumps(replacement).encode(), "synthetic-user")
        converter.CONFIG["trial_ledger"] = trial_rewards.TrialLedger(self.root / "trials.json")
        self.sync()
        self.claim.assert_called_once()

    def test_persistence_failure_does_not_block_balance_or_send_claim(self):
        self.configure()
        converter.CONFIG["auto_trial"] = True
        with patch.object(converter.CONFIG["trial_ledger"], "begin", side_effect=OSError("synthetic storage failure")):
            self.sync()
        self.claim.assert_not_called()
        self.assertEqual(self.order, ["credits"])

    def test_failed_claim_is_throttled_without_changing_balance(self):
        self.configure()
        converter.CONFIG["auto_trial"] = True
        self.claim.side_effect = None
        self.claim.return_value = {"ok": False, "already": False, "code": None, "status": None}
        self.sync()
        self.sync()
        self.claim.assert_called_once()
        self.assertEqual(self.order, ["credits", "credits"])


if __name__ == "__main__":
    unittest.main()
