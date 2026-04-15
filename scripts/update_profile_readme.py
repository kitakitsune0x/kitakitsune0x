#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


GITHUB_API_URL = "https://api.github.com"
README_PATH = Path(os.environ.get("README_PATH", "README.md"))
TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "github-stats.md"
START_MARKER = "<!--START_SECTION:github-stats-->"
END_MARKER = "<!--END_SECTION:github-stats-->"


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


class GitHubClient:
    def __init__(self, login: str, token: str):
        self.login = login
        self.headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "kitakitsune0x-profile-readme-updater",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        url = f"{GITHUB_API_URL}/{path.lstrip('/')}"
        if params:
            url = f"{url}?{urlencode(params)}"

        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")

        request = Request(url, data=data, headers=self.headers, method=method)
        with urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))

    def graphql(
        self,
        query: str,
        variables: dict[str, object],
    ) -> dict[str, object]:
        response = self.request(
            "POST",
            "graphql",
            payload={
                "query": query,
                "variables": variables,
            },
        )

        errors = response.get("errors")
        if isinstance(errors, list) and errors:
            messages = []
            for error in errors:
                if isinstance(error, dict):
                    message = error.get("message")
                    if isinstance(message, str):
                        messages.append(message)

            detail = "; ".join(messages) if messages else json.dumps(errors)
            raise RuntimeError(
                "GitHub GraphQL query failed: "
                f"{detail}. If this workflow is using the default GITHUB_TOKEN, "
                "set a PROFILE_README_TOKEN secret with a user token and rerun it."
            )

        data = response.get("data")
        if not isinstance(data, dict):
            raise RuntimeError(
                "GitHub GraphQL response did not include a data object. "
                f"Received keys: {', '.join(sorted(response.keys())) or '<none>'}"
            )

        return data

    def total_commits(self) -> int:
        response = self.request(
            "GET",
            "search/commits",
            params={
                "q": f"author:{self.login}",
            },
        )
        total_count = response.get("total_count")
        if total_count is None:
            raise RuntimeError("GitHub commit search response did not include total_count.")

        return int(total_count)


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def profile_repository_name(login: str) -> str:
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repository:
        _, repo_name = repository.split("/", 1)
        if repo_name:
            return repo_name

    return os.environ.get("PROFILE_REPOSITORY_NAME", login)


def fetch_stats(client: GitHubClient, repository_name: str) -> dict[str, int]:
    query = """
query ($login: String!, $profileRepositoryName: String!) {
  user(login: $login) {
    repositoriesContributedTo(
      first: 1
      contributionTypes: [COMMIT, ISSUE, PULL_REQUEST, REPOSITORY]
    ) {
      totalCount
    }
    pullRequests(first: 1) {
      totalCount
    }
    issues(first: 1) {
      totalCount
    }
    repository(name: $profileRepositoryName) {
      defaultBranchRef {
        target {
          ... on Commit {
            history(first: 0) {
              totalCount
            }
          }
        }
      }
    }
    repositories(
      first: 100
      ownerAffiliations: [OWNER]
      orderBy: {direction: DESC, field: STARGAZERS}
    ) {
      totalCount
      nodes {
        pullRequests {
          totalCount
        }
        issues {
          totalCount
        }
        stargazers {
          totalCount
        }
      }
    }
  }
  repository(owner: $login, name: $profileRepositoryName) {
    defaultBranchRef {
      target {
        ... on Commit {
          history(first: 0) {
            totalCount
          }
        }
      }
    }
  }
}
"""

    data = client.graphql(
        query,
        variables={
            "login": client.login,
            "profileRepositoryName": repository_name,
        },
    )

    user = data.get("user")
    if not isinstance(user, dict):
        raise RuntimeError(f"GitHub user '{client.login}' could not be loaded from GraphQL.")

    repositories = user["repositories"]["nodes"]
    repository = data.get("repository")
    profile_repo_history = (
        repository["defaultBranchRef"]["target"]["history"]["totalCount"]
        if repository and repository["defaultBranchRef"]
        else 0
    )

    return {
        "total_stars": sum(repo["stargazers"]["totalCount"] for repo in repositories),
        "total_commits": max(client.total_commits() - profile_repo_history, 0),
        "total_pull_requests": user["pullRequests"]["totalCount"]
        + sum(repo["pullRequests"]["totalCount"] for repo in repositories),
        "total_issues": user["issues"]["totalCount"]
        + sum(repo["issues"]["totalCount"] for repo in repositories),
        "contributed_to": user["repositoriesContributedTo"]["totalCount"]
        + user["repositories"]["totalCount"],
    }


def render_stats(stats: dict[str, int]) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    return template.format(**stats).rstrip()


def replace_section(content: str, new_section: str) -> str:
    start_index = content.find(START_MARKER)
    end_index = content.find(END_MARKER)

    if start_index == -1 or end_index == -1:
        raise RuntimeError("README markers for the GitHub stats section were not found.")

    start_index += len(START_MARKER)

    return (
        content[:start_index]
        + "\n"
        + new_section
        + "\n"
        + content[end_index:]
    )


def main() -> None:
    load_dotenv()

    login = require_env("GITHUB_LOGIN")
    token = require_env("GITHUB_TOKEN")

    client = GitHubClient(login=login, token=token)
    stats = fetch_stats(client, profile_repository_name(login))
    rendered_stats = render_stats(stats)

    current_readme = README_PATH.read_text(encoding="utf-8")
    updated_readme = replace_section(current_readme, rendered_stats)

    if updated_readme == current_readme:
        print(f"{README_PATH} is already up to date")
        return

    README_PATH.write_text(updated_readme, encoding="utf-8")
    print(f"Updated {README_PATH}")


if __name__ == "__main__":
    main()
