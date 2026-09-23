"""Offline checks of deployment rejection paths; no Google calls or credentials."""

import argparse
import json
import os
from types import SimpleNamespace
import unittest
from unittest import mock

from check_target import (TASK_CREATED_AFTER, make_parser, read_json, validate_auth_configuration, validate_auth_environment,
                          validate_inputs, verify_org_and_billing, verify_project)


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.args = argparse.Namespace(new_org="123456789012", project="isolated-hub-test",
                                       billing_account="AAAAAA-BBBBBB-CCCCCC", account="operator@new.example",
                                       expected_domain="new.example", expected_org_name=None,
                                       isolation="organization", credential_file=None, created_after=TASK_CREATED_AFTER)
        self.org = {"name": "organizations/123456789012", "lifecycleState": "ACTIVE", "displayName": "new.example"}
        self.billing = {"name": "billingAccounts/AAAAAA-BBBBBB-CCCCCC", "open": True, "parent": "organizations/123456789012"}
        self.project = {"projectId": "isolated-hub-test", "lifecycleState": "ACTIVE",
                        "parent": {"type": "organization", "id": "123456789012"}, "labels": {"agent-hub-isolated": "true"}}
        self.billing_info = {"billingEnabled": True, "billingAccountName": self.billing["name"]}

    def test_selected_new_org_target_passes(self):
        validate_inputs(self.args)
        verify_org_and_billing(self.args, self.org, self.billing)
        verify_project(self.args, self.project, self.billing_info)

    def test_known_old_org_always_rejected(self):
        for organization in ("833782852711", "1091262552755"):
            with self.subTest(organization=organization), self.assertRaises(ValueError):
                self.args.new_org = organization
                validate_inputs(self.args)

    def test_old_identity_rejected(self):
        self.args.account = "operator@old.example"
        with self.assertRaises(ValueError):
            validate_inputs(self.args)

    def test_wrong_domain_and_billing_rejected(self):
        for field, value in [("displayName", "old.example"), ("name", "organizations/833782852711"), ("lifecycleState", "DELETE_REQUESTED")]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                verify_org_and_billing(self.args, {**self.org, field: value}, self.billing)
        for field, value in [("parent", "organizations/833782852711"), ("parent", ""), ("open", False)]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                verify_org_and_billing(self.args, self.org, {**self.billing, field: value})

    def test_project_without_exact_parent_rejected(self):
        for parent in [{}, {"type": "organization", "id": "833782852711"}, {"type": "folder", "id": "123456789012"}]:
            with self.subTest(parent=parent), self.assertRaises(ValueError):
                verify_project(self.args, {**self.project, "parent": parent}, self.billing_info)

    def test_unlabeled_and_inactive_project_rejected(self):
        for field, value in [("labels", {}), ("lifecycleState", "DELETE_REQUESTED"), ("projectId", "existing-project")]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                verify_project(self.args, {**self.project, field: value}, self.billing_info)

    def test_wrong_project_billing_rejected(self):
        for field, value in [("billingEnabled", False), ("billingAccountName", "billingAccounts/XXXXXX-YYYYYY-ZZZZZZ")]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                verify_project(self.args, self.project, {**self.billing_info, field: value})

    def test_plain_user_login_auth_configuration_is_accepted(self):
        validate_auth_environment({"PATH": "sdk-bin", "CLOUDSDK_CONFIG": "new-isolated-config",
                                   "CLOUDSDK_ACTIVE_CONFIG_NAME": "new-org"})
        for config in ({}, {"auth": {}}, {"auth": {"disable_credentials": False}},
                       {"auth": {"disable_credentials": "false", "access_token_file": None}}):
            validate_auth_configuration(config)

    def test_auth_environment_overrides_rejected_without_showing_values(self):
        for name in ("CLOUDSDK_AUTH_ACCESS_TOKEN", "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE",
                     "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE", "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT",
                     "CLOUDSDK_AUTH_DISABLE_CREDENTIALS", "cloudsdk_auth_future_override"):
            with self.subTest(name=name), self.assertRaises(ValueError) as rejected:
                validate_auth_environment({name: "do-not-display-this-secret"})
            self.assertNotIn("do-not-display-this-secret", str(rejected.exception))

    def test_configured_auth_overrides_rejected_without_showing_values(self):
        for name in ("access_token_file", "credential_file_override", "impersonate_service_account",
                     "authorization_token_file", "disable_credentials", "login_config_file", "token_host",
                     "future_auth_override"):
            with self.subTest(name=name), self.assertRaises(ValueError) as rejected:
                validate_auth_configuration({"auth": {name: "do-not-display-this-secret"}})
            self.assertNotIn("do-not-display-this-secret", str(rejected.exception))
        for config in ({"auth": []}, {"auth": None}, {"unexpected": {}}):
            with self.assertRaises(ValueError):
                validate_auth_configuration(config)

    def test_auth_environment_override_stops_before_any_gcloud_process(self):
        with mock.patch.dict("os.environ", {"CLOUDSDK_AUTH_ACCESS_TOKEN": "secret"}, clear=True), \
                mock.patch("check_target.subprocess.run") as run:
            with self.assertRaises(ValueError):
                read_json(self.args, "organizations", "describe", self.args.new_org)
            run.assert_not_called()

    def test_auth_config_override_stops_before_cloud_read(self):
        result = SimpleNamespace(returncode=0, stdout=json.dumps({"auth": {"impersonate_service_account": "old-secret-principal"}}), stderr="")
        with mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch("check_target.shutil.which", return_value="gcloud"), \
                mock.patch("check_target.subprocess.run", return_value=result) as run:
            with self.assertRaises(ValueError) as rejected:
                read_json(self.args, "organizations", "describe", self.args.new_org)
            self.assertNotIn("old-secret-principal", str(rejected.exception))
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][1:4], ["config", "list", "auth/"])

    def test_plain_login_checks_auth_before_each_api_read_with_same_environment(self):
        responses = [SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")
                     for data in ({"auth": {}}, self.org, {"auth": {}}, self.billing)]
        environment = {"CLOUDSDK_CONFIG": "new-isolated-config", "PATH": "sdk-bin"}
        with mock.patch.dict("os.environ", environment, clear=True), \
                mock.patch("check_target.shutil.which", return_value="gcloud"), \
                mock.patch("check_target.subprocess.run", side_effect=responses) as run:
            self.assertEqual(read_json(self.args, "organizations", "describe", self.args.new_org), self.org)
            self.assertEqual(read_json(self.args, "billing", "accounts", "describe", self.args.billing_account), self.billing)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual([command[1] for command in commands], ["config", "organizations", "config", "billing"])
        for call in run.call_args_list:
            self.assertIn("--account=operator@new.example", call.args[0])
            self.assertIn("--project=isolated-hub-test", call.args[0])
            self.assertFalse(call.kwargs["shell"])
            self.assertEqual(call.kwargs["env"], environment)
        self.assertIs(run.call_args_list[0].kwargs["env"], run.call_args_list[1].kwargs["env"])

    def test_failed_or_malformed_auth_inspection_fails_closed_without_raw_output(self):
        responses = (SimpleNamespace(returncode=1, stdout="secret-token", stderr="secret-provider-diagnostic"),
                     SimpleNamespace(returncode=0, stdout="secret-malformed-json", stderr=""),
                     SimpleNamespace(returncode=0, stdout="[]", stderr=""))
        for result in responses:
            with self.subTest(stdout=result.stdout), mock.patch.dict("os.environ", {}, clear=True), \
                    mock.patch("check_target.shutil.which", return_value="gcloud"), \
                    mock.patch("check_target.subprocess.run", return_value=result) as run:
                with self.assertRaises(ValueError) as rejected:
                    read_json(self.args, "organizations", "describe", self.args.new_org)
                self.assertNotIn("secret", str(rejected.exception))
                self.assertEqual(run.call_count, 1)

    def test_override_enabled_between_reads_is_rejected(self):
        responses = [SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")
                     for data in ({"auth": {}}, self.org, {"auth": {"credential_file_override": "secret.json"}})]
        with mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch("check_target.shutil.which", return_value="gcloud"), \
                mock.patch("check_target.subprocess.run", side_effect=responses) as run:
            read_json(self.args, "organizations", "describe", self.args.new_org)
            with self.assertRaises(ValueError):
                read_json(self.args, "billing", "accounts", "describe", self.args.billing_account)
            self.assertEqual(run.call_count, 3)

    def project_mode(self):
        self.args.isolation = "project"
        self.args.new_org = "833782852711"
        self.args.account = "existing-owner@example.net"
        self.args.expected_domain = None
        self.args.expected_org_name = "Existing verified organization"
        self.org.update(name="organizations/833782852711", displayName=self.args.expected_org_name)
        self.billing["parent"] = self.org["name"]
        self.project["parent"]["id"] = self.args.new_org
        self.project["createTime"] = "2026-09-20T12:00:00Z"

    def test_explicit_project_mode_accepts_new_project_in_exact_existing_org(self):
        self.project_mode()
        validate_inputs(self.args)
        verify_org_and_billing(self.args, self.org, self.billing)
        verify_project(self.args, self.project, self.billing_info)

    def test_project_mode_can_select_each_existing_org_but_not_different_metadata(self):
        self.project_mode()
        for organization in ("833782852711", "1091262552755"):
            self.args.new_org = organization
            validate_inputs(self.args)
        self.args.new_org = "833782852711"
        for wrong in ({**self.org, "name": "organizations/1091262552755"},
                      {**self.org, "displayName": "Different organization"}):
            with self.assertRaises(ValueError):
                verify_org_and_billing(self.args, wrong, self.billing)
        with self.assertRaises(ValueError):
            verify_org_and_billing(self.args, self.org, {**self.billing, "parent": "organizations/1091262552755"})

    def test_project_mode_still_rejects_old_or_undated_project(self):
        self.project_mode()
        for value in (None, "2026-09-19T23:59:59Z", "invalid", "2026-09-20T12:00:00"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                verify_project(self.args, {**self.project, "createTime": value}, self.billing_info)
        self.args.created_after = "2020-01-01T00:00:00Z"
        with self.assertRaises(ValueError):
            validate_inputs(self.args)

    def test_exact_authorized_credential_file_is_allowed_only_in_project_mode(self):
        expected = os.path.abspath("approved-test-credential.json")
        validate_auth_environment({"CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE": expected}, expected)
        validate_auth_configuration({"auth": {"credential_file_override": expected}}, expected)
        self.args.credential_file = expected
        with self.assertRaises(ValueError):
            validate_inputs(self.args)
        self.project_mode()
        validate_inputs(self.args)

    def test_credential_file_mismatch_and_other_overrides_remain_forbidden(self):
        expected = os.path.abspath("approved-test-credential.json")
        different = os.path.abspath("different-private-credential.json")
        with self.assertRaises(ValueError) as rejected:
            validate_auth_environment({"CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE": different}, expected)
        self.assertNotIn(different, str(rejected.exception))
        with self.assertRaises(ValueError):
            validate_auth_configuration({"auth": {"credential_file_override": different}}, expected)
        for name in ("access_token_file", "impersonate_service_account", "future_auth_override"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_auth_configuration({"auth": {"credential_file_override": expected, name: "secret"}}, expected)
        with self.assertRaises(ValueError):
            validate_auth_environment({"CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE": expected, "CLOUDSDK_AUTH_ACCESS_TOKEN": "secret"}, expected)

    def test_pinned_credential_file_must_be_active_no_silent_login_fallback(self):
        self.project_mode()
        self.args.credential_file = os.path.abspath("approved-test-credential.json")
        response = SimpleNamespace(returncode=0, stdout='{"auth":{}}', stderr="")
        with mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch("check_target.shutil.which", return_value="gcloud"), \
                mock.patch("check_target.subprocess.run", return_value=response) as run:
            with self.assertRaises(ValueError):
                read_json(self.args, "organizations", "describe", self.args.new_org)
            self.assertEqual(run.call_count, 1)

    def test_pinned_credential_environment_and_exact_account_reach_read_only_call(self):
        self.project_mode()
        self.args.credential_file = os.path.abspath("approved-test-credential.json")
        env = {"CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE": self.args.credential_file}
        responses = [SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="") for data in ({"auth": {}}, self.org)]
        with mock.patch.dict("os.environ", env, clear=True), \
                mock.patch("check_target.shutil.which", return_value="gcloud"), \
                mock.patch("check_target.subprocess.run", side_effect=responses) as run:
            self.assertEqual(read_json(self.args, "organizations", "describe", self.args.new_org), self.org)
            for call in run.call_args_list:
                self.assertIn("--account=existing-owner@example.net", call.args[0])
                self.assertEqual(call.kwargs["env"], env)
                self.assertNotIn(self.args.credential_file, call.args[0])

    def test_cli_identity_selectors_are_mutually_exclusive_and_account_explicit(self):
        parser = make_parser()
        required = ["--organization", "833782852711", "--project", "isolated-hub-test",
                    "--billing-account", "AAAAAA-BBBBBB-CCCCCC", "--account", "existing-owner@example.net",
                    "--isolation", "project", "--expected-org-name", "Existing verified organization"]
        parsed = parser.parse_args(required)
        validate_inputs(parsed)
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            parser.parse_args(required + ["--expected-domain", "new.example"])
        without_account = [item for item in required if item not in ("--account", "existing-owner@example.net")]
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            parser.parse_args(without_account)


if __name__ == "__main__":
    unittest.main()
