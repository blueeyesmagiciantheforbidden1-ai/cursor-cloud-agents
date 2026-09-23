"""Verify metadata assumptions in the exact downloaded public release, offline."""
import hashlib
import json
from pathlib import Path
import tarfile

from build_native import validate_release, verify_archive
from prepare_release import OBSERVED_SHA256, VERSION

HERE = Path(__file__).resolve().parent
CONTRACT = {
    'cursor-agent': {
        'vendor_entrypoint_uses_bundled_node': 'NODE_BIN="$SCRIPT_DIR/node"',
        'vendor_entrypoint_executes_index': '"$SCRIPT_DIR/index.js" "$@"',
        'file_store_skips_system_ca_probe': 'AGENT_CLI_CREDENTIAL_STORE',
    },
    '5589.index.js': {
        'models_calls_native_api_auth': 'yield(0,r.E)(e,{endpoint:n.endpoint,apiKey:n.apiKey,authToken:n.authToken})',
        'models_fetches_account_catalog': 'yield(0,h.aC)(g,',
        'bracket_model_overrides_supported': 'claude-opus-4-8[context=1m,effort=high,fast=false]',
    },
    '4835.index.js': {
        'native_supported_api_key_exchange': 'loginWithApiKey(d,{endpoint:n.endpoint})',
        'native_access_refresh_persistence': 'e.setAuthentication(n,t,d)',
    },
    '3837.index.js': {
        'status_requires_access_and_refresh': 'if(s&&a)try',
        'status_fetches_fresh_getMe': 'yield t.getMe(new n.GetMeRequest({}))',
        'status_getMe_failure_has_no_identity': 'Logged in (unable to fetch user details)',
    },
    '1699.index.js': {
        'preauthenticated_acp_supports_api_key': 'process.env.CURSOR_API_KEY',
        'no_session_model_extension': 'cursor/list_available_models',
        'catalog_uses_parameterized_mode': 'this.getAcpAvailableModels("parameterized")',
        'catalog_projects_model_and_config_options': 'value:e.name,name:e.clientDisplayName||e.name,configOptions:this.buildModelParameterConfigOptions(e,t)',
        'single_value_parameters_hidden': 'if(i.length<2)return[]',
        'effort_category_detected': 'this.isThoughtLevelParameter(e)?"thought_level"',
        'empty_catalog_on_fetch_error': '"ACP fetchAvailableModelsForAcp failed",{error:e}),[]',
        'browser_authenticate_must_not_be_called': '"Starting browser login flow"',
    },
    '9009.index.js': {
        'sea_reexec_is_macos_computer_use_only': 'return"darwin"===e.platform&&e.computerUse&&!e.alreadyReexec&&!e.allowUnsigned',
    },
    'index.js': {
        'mcp_loader_reads_local_home_and_workspace': 'i.join((0,s.homedir)(),".cursor","mcp.json")',
        'custom_config_directory_supported': 'process.env.CURSOR_CONFIG_DIR',
        'file_credential_store_supported': 'AGENT_CLI_CREDENTIAL_STORE',
    },
}


def main():
    archive_path = HERE / '.cache' / 'agent-cli-package.tar.gz'
    receipt = validate_release(json.loads((HERE / 'provenance/release.json').read_text()))
    verify_archive(archive_path, receipt)
    observations = {}
    with tarfile.open(archive_path, 'r:gz') as archive:
        for name, checks in CONTRACT.items():
            data = archive.extractfile('dist-package/' + name).read()
            source = data.decode('utf-8')
            found = {claim: source.find(needle) for claim, needle in checks.items()}
            if any(offset < 0 for offset in found.values()):
                raise ValueError('Pinned public source contract changed: ' + name)
            observations[name] = {'sha256': hashlib.sha256(data).hexdigest(),
                                  'bytes': len(data), 'verified_source_offsets': found}
    report = {'schema_version': 1, 'version': VERSION, 'archive_sha256': OBSERVED_SHA256,
              'source_only': True, 'executed': False, 'observations': observations,
              'sea_execution_equivalence_verified': False,
              'supported_entrypoint': 'cursor-agent -> bundled node -> index.js',
              'fixed_composer_effort_proven': False,
              'metadata_commands': [['models'], ['status', '--format', 'json'], ['acp']],
              'acp_methods': ['initialize', 'cursor/list_available_models']}
    (HERE / 'provenance' / 'metadata-contract.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'source_contract_checks': sum(map(len, CONTRACT.values())), 'executed': False}))


if __name__ == '__main__':
    main()
