import shlex

import nox


@nox.session(
    python=[
        "3.9",
        "3.10",
        "3.11",
    ]
)
def test(session):
    session.install(".")
    session.install("pytest")
    session.run("pytest")


@nox.session(
    python=[
        "3.9",
        "3.10",
        "3.11",
    ]
)
def testcov(session):
    session.install(".")
    session.install("pytest", "pytest-html", "coverage")
    session.run(
        *shlex.split(
            "coverage run --concurrency=multiprocessing,thread "
            "-m pytest "
            " --html=report.html --self-contained-html"
        )
    )
    session.run(*shlex.split("coverage combine"))
    session.run(*shlex.split("coverage report"))
    session.run(*shlex.split("coverage html"))


@nox.session(tags=["style", "fix"], python=False)
def black(session):
    # session.install("black")
    session.run("black", ".")


@nox.session(tags=["style", "fix"], python=False)
def isort(session):
    # session.install("isort")
    session.run("isort", "--profile", "black", ".")


@nox.session(tags=["style"], python=False)
@nox.session
def lint(session):
    # session.install("ruff")
    session.run("ruff", ".")
