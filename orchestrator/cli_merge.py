import typer


class CliMergeBackend:
    """A MergeBackend that asks at the terminal."""

    def wait_for_merge(self, pr_urls: list[str]) -> bool:
        typer.echo("QA checks the finished project, so these pull requests must be merged first:")
        for url in pr_urls:
            typer.echo(f"  {url}")
        return typer.confirm("Have you merged them? Check again", default=True)
