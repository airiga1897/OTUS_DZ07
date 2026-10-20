"""Public Selectel lifecycle: deploy, verify and destroy."""
import argparse


def run(action, provider, packages):
    provider.auth()
    provider.lab.output, provider.lab.trust = provider.output, provider.trust
    if action == 'destroy':
        provider.cleanup()
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
        provider.lab.verify()
        if action == 'verify':
            provider.failover()  # Restarts the stopped node and waits for green.
            provider.lab.verify()  # Application and ingestion after recovery.
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
    run(action, selectel, cache_packages)


if __name__ == '__main__':
    main()
