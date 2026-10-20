"""Public Selectel lifecycle: deploy, verify and destroy."""
import argparse
import sys
import privacy


def run(action, provider, packages):
    provider.auth()
    provider.lab.output, provider.lab.trust = provider.output, provider.trust
    if action == 'destroy':
        provider.cleanup()
        print('DESTROY CONFIRMED', flush=True)
        return
    # Preparation cannot create billable infrastructure. Preserve any existing
    # deployment if credentials, downloads or configuration checks fail here.
    if action == 'deploy':
        provider.prepare()
        packages.main()
    try:
        if action == 'deploy':
            provider.tf('apply', '-input=false', '-no-color', str(provider.plan()))
            provider.lab.configure()
            provider.lab.save(provider.LOCAL / 'lab.json', provider.output())
        provider.lab.verify()
        if action == 'verify':
            provider.failover()  # Restarts the stopped node and waits for green.
            provider.lab.verify()  # Application and ingestion after recovery.
        print('APPLICATION READY' if action == 'deploy' else 'LIVE VERIFICATION PASSED', flush=True)
    except BaseException:
        try:
            provider.diagnostics()
        finally:
            provider.cleanup()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['deploy', 'verify', 'destroy'])
    action = parser.parse_args().action
    import selectel
    import cache_packages
    try:
        run(action, selectel, cache_packages)
    except Exception as error:
        print('ERROR: ' + privacy.redact(str(error)), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
