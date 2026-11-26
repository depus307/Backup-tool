"""Single GUI/frozen entry point; internal smoke flags are not a public CLI."""
import multiprocessing

if __name__ == '__main__':
    multiprocessing.freeze_support()
    import argparse

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--internal-smoke-report')
    parser.add_argument('--internal-smoke-scenario', choices=('normal', 'cancel'), default='normal')
    parser.add_argument('--internal-job-file')
    parser.add_argument('--internal-job-report')
    arguments = parser.parse_args()
    if arguments.internal_job_file and arguments.internal_job_report:
        from backuptool.frozen_probe import execute
        execute(arguments.internal_job_file, arguments.internal_job_report)
    else:
        from backuptool.ui import run_shell
        run_shell(arguments.internal_smoke_report, arguments.internal_smoke_scenario)
