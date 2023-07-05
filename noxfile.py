import nox


@nox.session(
    python=[
        "3.9",
        "3.10",
        "3.11",
    ]
)
def tests(session):
    session.install(".")
    session.install("pytest")
    session.run("pytest")


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
