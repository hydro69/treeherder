import logging

import newrelic.agent
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist

from treeherder.etl.common import (fetch_json,
                                   to_timestamp)
from treeherder.model.derived.jobs import JobsModel
from treeherder.model.models import Repository

logger = logging.getLogger(__name__)


class ResultsetLoader:
    """Transform and load a list of Resultsets"""

    def process(self, message_body, exchange):
        logger.info("Begin resultset processing")
        try:
            transformer = self.get_transformer_class(exchange)(message_body)

            # TODO: We have the same url for several repos, like gaia.
            # So we may want to store the branch to determine which repo
            # is right.  Or perhaps there's another way.  Check with garndt
            # when he gets back.

            logger.warn(transformer.repo_url)
            # todo: can't just use .first() here.  need to get the right one
            repo = Repository.objects.filter(url=transformer.repo_url,
                                             active_status="active").first()
            transformed_data = transformer.transform(repo.name)

            with JobsModel(repo.name) as jobs_model:
                jobs_model.store_result_set_data([transformed_data])

        except ObjectDoesNotExist:
            newrelic.agent.record_custom_event("skip_unknown_repository",
                                               message_body["details"])
            logger.warn("Skipping unsupported repo: {}".format(
                transformer.repo_url))
        except Exception as ex:
            newrelic.agent.record_exception(exc=ex)

    def get_transformer_class(self, exchange):
        if "github" in exchange:
            if exchange.endswith("push"):
                return GithubPushTransformer
            elif exchange.endswith("pull-request"):
                return GithubPullRequestTransformer
        raise PulseResultsetError(
            "Unsupported resultset type: {}".format(exchange))


class GithubTransformer:

    CREDENTIALS = {
        "client_id": settings.GITHUB_CLIENT_ID,
        "client_secret": settings.GITHUB_CLIENT_SECRET
    }

    def __init__(self, message_body):
        self.message_body = message_body
        self.repo_url = message_body["details"]["event.head.repo.url"].replace(
                ".git", "")

    def fetch_resultset(self, url, repository, sha=None):
        params = {"sha": sha} if sha else {}
        params.update(self.CREDENTIALS)

        try:
            commits = self.get_cleaned_commits(fetch_json(url, params))
            head_commit = commits[0]
            resultset = {
                "revision": head_commit["sha"],
                "push_timestamp": to_timestamp(
                    head_commit["commit"]["author"]["date"]),
                "author": head_commit["commit"]["author"]["email"],
            }

            revisions = []
            for commit in commits:
                revisions.append({
                    "comment": commit["commit"]["message"],
                    "repository": repository,
                    "author": "{} <{}>".format(
                        commit["commit"]["author"]["name"],
                        commit["commit"]["author"]["email"]),
                    "revision": commit["sha"]
                })

            resultset["revisions"] = revisions
            return resultset

        except Exception as ex:
            logger.exception("Error fetching commits", exc_info=ex)
            newrelic.agent.record_exception(ex, params={
                "url": url, "repository": repository, "sha": sha
                })

    def get_cleaned_commits(self, commits):
        """Allow a subclass to change the order of the commits"""
        return commits


class GithubPushTransformer(GithubTransformer):
    # {
    #     organization:mozilla - services
    #     details:{
    #         event.type:push
    #         event.base.repo.branch:master
    #         event.head.repo.branch:master
    #         event.head.user.login:mozilla-cloudops-deploy
    #         event.head.repo.url:https://github.com/mozilla-services/cloudops-jenkins.git
    #         event.head.sha:845aa1c93726af92accd9b748ea361a37d5238b6
    #         event.head.ref:refs/heads/master
    #         event.head.user.email:mozilla-cloudops-deploy@noreply.github.com
    #     }
    #     repository:cloudops-jenkins
    #     version:1
    # }

    URL_BASE = "https://api.github.com/repos/{}/{}/commits"

    def transform(self, repository):
        commit = self.message_body["details"]["event.head.sha"]
        push_url = self.URL_BASE.format(
            self.message_body["organization"],
            self.message_body["repository"]
        )
        return self.fetch_resultset(push_url, repository, sha=commit)

    def get_cleaned_commits(self, commits):
        # todo: won't need the get() with default once garndt updates the pulse
        # messages with the value
        base_sha = self.message_body["details"].get("event.base.sha", "")
        for idx, commit in enumerate(commits):
            if commit["sha"] == base_sha:
                return commits[:idx]
        return commits


class GithubPullRequestTransformer(GithubTransformer):
    # {
    #     "organization": "mozilla",
    #     "action": "synchronize",
    #     "details": {
    #         "event.type": "pull_request.synchronize",
    #         "event.base.repo.branch": "master",
    #         "event.pullNumber": "1692",
    #         "event.base.user.login": "mozilla",
    #         "event.base.repo.url": "https: // github.com / mozilla / treeherder.git",
    #         "event.base.sha": "ff6a66a27c2c234e5820b8ffe48f17d85f1eb2db",
    #         "event.base.ref": "master",
    #         "event.head.user.login": "mozilla",
    #         "event.head.repo.url": "https: // github.com / mozilla / treeherder.git",
    #         "event.head.repo.branch": "github - pulse - resultsets",
    #         "event.head.sha": "0efea0fa1396369b5058e16139a8ab51cdd7bd29",
    #         "event.head.ref": "github - pulse - resultsets",
    #         "event.head.user.email": "mozilla@noreply.github.com",
    #     },
    #     "repository": "treeherder",
    #     "version": 1
    # }

    URL_BASE = "https://api.github.com/repos/{}/{}/pulls/{}/commits"

    def transform(self, repository):
        pr_url = self.URL_BASE.format(
            self.message_body["organization"],
            self.message_body["repository"],
            self.message_body["details"]["event.pullNumber"]
        )

        return self.fetch_resultset(pr_url, repository)

    def get_cleaned_commits(self, commits):
        """
        Pull requests need the order of their commits reversed.
        """
        return list(reversed(commits))


class PulseResultsetError(ValueError):
    pass
