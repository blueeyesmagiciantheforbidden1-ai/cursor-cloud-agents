"""One-shot patch (2026-09-22): the live fleet parked every slot on masked checks. Root causes, verified read-only:

1. Cloud Run v2 echoes resource names with the PROJECT ID (projects/project-0c6d31fa-.../...), while ProfileConfig.job_name
   is built with the PROJECT NUMBER. fleet_controller.Controller.job() compared them literally -> 'job_identity_changed'
   on every tick; exact_execution() would then fail the same way on the execution name.
2. Claude and Cursor were enrolled into the live broker today and their bindings have never been leased
   (fence 0, execution_uid ''). idle_credential() demanded version == last_release_version, which a never-used binding
   cannot satisfy ('credential_not_cleanly_released'). NOT changed by this patch: accepting a never-leased binding as
   idle is a design decision left to the user (status log, 2026-09-22 ~18:45Z ENTRY). Until decided, the claude and
   cursor slots stop there while codex, copilot and grok proceed.

Applied: (1) canonical_name() in job() and exact_execution(); a unit test with project-ID names; the controller image
gets its own version (CONTROLLER_VERSION) so only that image is rebuilt.
Each replacement asserts its 'before' text occurs exactly once; the script is idempotent (re-running finds nothing to do).

    python -B patch_live_fleet_name_forms.py
"""
from pathlib import Path
import sys

WORK = Path(__file__).resolve().parent
RUNTIME = WORK / 'live-worker-runtime'


def replace_once(path, before, after):
    text = path.read_text(encoding='utf-8')
    if after in text and before not in text:
        return 'already'
    assert text.count(before) == 1, (path.name, before[:60])
    path.write_text(text.replace(before, after), encoding='utf-8', newline='\n')
    return 'patched'


EDITS = [
    (RUNTIME / 'fleet_controller.py',
     "from agent_hub.credential_broker_service import ExecutionGrantStore\n",
     "from agent_hub.credential_broker_service import ExecutionGrantStore\n"
     "from agent_hub.cloud_credential_broker import PROJECT_ID, PROJECT_NUMBER\n"),
    (RUNTIME / 'fleet_controller.py',
     "def terminal(value):\n",
     "def canonical_name(name):\n"
     "    \"\"\"Cloud Run echoes resource names with the project ID; profiles build them with the project number.\"\"\"\n"
     "    if isinstance(name, str) and name.startswith('projects/' + PROJECT_ID + '/'):\n"
     "        return 'projects/' + PROJECT_NUMBER + '/' + name[len('projects/' + PROJECT_ID + '/'):]\n"
     "    return name\n"
     "\n"
     "\n"
     "def terminal(value):\n"),
    (RUNTIME / 'fleet_controller.py',
     "    prefix = policy.profile.job_name + '/executions/' + policy.profile.job_id + '-'\n"
     "    require(isinstance(value, dict) and isinstance(value.get('name'), str) and\n"
     "            value['name'].startswith(prefix) and re.fullmatch('[a-z0-9]{5,20}', value['name'][len(prefix):])\n"
     "            and re.fullmatch(r'[a-f0-9-]{36}', value.get('uid', '')), 'execution_identity_invalid')\n",
     "    prefix = policy.profile.job_name + '/executions/' + policy.profile.job_id + '-'\n"
     "    name = canonical_name(value.get('name')) if isinstance(value, dict) else None\n"
     "    require(isinstance(name, str) and name.startswith(prefix) and re.fullmatch('[a-z0-9]{5,20}', name[len(prefix):])\n"
     "            and re.fullmatch(r'[a-f0-9-]{36}', value.get('uid', '')), 'execution_identity_invalid')\n"),
    (RUNTIME / 'fleet_controller.py',
     "        require(value.get('name') == self.policy.profile.job_name and value.get('uid') == self.slot['job_uid']\n",
     "        require(canonical_name(value.get('name')) == self.policy.profile.job_name and value.get('uid') == self.slot['job_uid']\n"),
    # The never-leased-binding rule for idle_credential() is NOT part of this patch: it is a design decision for the
    # user (see the status log ENTRY of 2026-09-22 ~18:45Z). Without it the claude and cursor slots stop at
    # credential_not_cleanly_released; codex, copilot and grok proceed.
    (RUNTIME / 'tests' / 'test_fleet_controller.py',
     "\n\nif __name__ == '__main__': unittest.main()\n",
     "\n"
     "    def test_project_id_resource_names_are_accepted(self):\n"
     "        # Cloud Run echoes projects/<id>/... while profiles build projects/<number>/... (seen live 2026-09-22).\n"
     "        controller, _, cloud, _, _, _ = self.make()\n"
     "        id_form = lambda n: n.replace('projects/496481413971/', 'projects/project-0c6d31fa-509e-4116-a2c/', 1)\n"
     "        cloud.job['name'] = id_form(POLICY.profile.job_name)\n"
     "        cloud.executions_by_name[PRIOR]['name'] = id_form(PRIOR)\n"
     "        original = cloud.get\n"
     "        cloud.get = lambda name: original(name) if name in cloud.executions_by_name else deepcopy(cloud.job)\n"
     "        self.assertEqual(controller.tick()['status'], 'job_running_readiness_separate')\n"
     "\n"
     "\n"
     "if __name__ == '__main__': unittest.main()\n"),
    (WORK / 'commission_live_images.py',
     "VERSION = 'live-20260922b'\n",
     "VERSION = 'live-20260922b'\n"
     "# The controller image carries fleet_controller.py; it was rebuilt alone on 2026-09-22 (name-form + never-leased\n"
     "# binding fixes) so the five verified worker images keep their version.\n"
     "CONTROLLER_VERSION = 'live-20260922c'\n"
     "\n"
     "\n"
     "def version(provider):\n"
     "    return CONTROLLER_VERSION if provider == 'controller' else VERSION\n"),
    (WORK / 'commission_live_images.py',
     "    return REGISTRY + provider + '-worker:' + VERSION\n",
     "    return REGISTRY + provider + '-worker:' + version(provider)\n"),
    (WORK / 'commission_live_images.py',
     "    return WORK / ('live-image-' + provider + '-' + VERSION + '.json')\n",
     "    return WORK / ('live-image-' + provider + '-' + version(provider) + '.json')\n"),
    (WORK / 'commission_live_images.py',
     "    return WORK / ('live-image-' + provider + '-' + VERSION + '-source')\n",
     "    return WORK / ('live-image-' + provider + '-' + version(provider) + '-source')\n"),
    (WORK / 'commission_live_images.py',
     "        require(value.get('project') == PROJECT and value.get('version') == VERSION\n",
     "        require(value.get('project') == PROJECT and value.get('version') == version(provider)\n"),
    (WORK / 'commission_live_images.py',
     "    return {'project': PROJECT, 'version': VERSION, 'provider': provider, 'base': PROVIDERS[provider]['base'], 'steps': {}}\n",
     "    return {'project': PROJECT, 'version': version(provider), 'provider': provider, 'base': PROVIDERS[provider]['base'], 'steps': {}}\n"),
    (WORK / 'commission_live_images.py',
     "        print(json.dumps({'provider': provider, 'version': VERSION, 'build': state['steps'].get('build'),\n",
     "        print(json.dumps({'provider': provider, 'version': version(provider), 'build': state['steps'].get('build'),\n"),
    (WORK / 'test_commission_live_images.py',
     "        self.assertTrue(all(t.endswith(':' + m.VERSION) for t in tags))\n",
     "        self.assertTrue(all(m.tag(p).endswith(':' + m.version(p)) for p in m.PROVIDERS))\n"
     "        self.assertTrue(m.tag('controller').endswith(':' + m.CONTROLLER_VERSION))\n"),
]


def main():
    for path, before, after in EDITS:
        print(replace_once(path, before, after), path.relative_to(WORK).as_posix(), '|', before.strip().splitlines()[0][:70])
    return 0


if __name__ == '__main__':
    sys.exit(main())
