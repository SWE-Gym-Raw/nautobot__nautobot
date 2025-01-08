"""Models for representing external data sources."""

from importlib.util import find_spec
import os

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models

from nautobot.core.constants import CHARFIELD_MAX_LENGTH
from nautobot.core.models.fields import AutoSlugField, LaxURLField, slugify_dashes_to_underscores
from nautobot.core.models.generics import PrimaryModel
from nautobot.core.models.validators import EnhancedURLValidator
from nautobot.extras.utils import check_if_key_is_graphql_safe, extras_features


@extras_features(
    "config_context_owners",
    "export_template_owners",
    "graphql",
    "job_results",
    "webhooks",
)
class GitRepository(PrimaryModel):
    """Representation of a Git repository used as an external data source."""

    name = models.CharField(
        max_length=CHARFIELD_MAX_LENGTH,
        unique=True,
    )
    slug = AutoSlugField(
        populate_from="name",
        help_text="Internal field name. Please use underscores rather than dashes in this key.",
        slugify_function=slugify_dashes_to_underscores,
    )

    remote_url = LaxURLField(
        max_length=CHARFIELD_MAX_LENGTH,
        # For the moment we don't support ssh:// and git:// URLs
        help_text="Only HTTP and HTTPS URLs are presently supported",
        validators=[EnhancedURLValidator(schemes=["http", "https"])],
    )
    branch = models.CharField(
        max_length=CHARFIELD_MAX_LENGTH,
        default="main",
        help_text="Branch, tag, or commit",
    )

    current_head = models.CharField(
        help_text="Commit hash of the most recent fetch from the selected branch. Used for syncing between workers.",
        max_length=48,
        default="",
        blank=True,
    )

    secrets_group = models.ForeignKey(
        to="extras.SecretsGroup",
        on_delete=models.SET_NULL,
        default=None,
        blank=True,
        null=True,
        related_name="git_repositories",
    )

    # Data content types that this repo is a source of. Valid options are dynamically generated based on
    # the data types registered in registry['datasource_contents'].
    provided_contents = models.JSONField(encoder=DjangoJSONEncoder, default=list, blank=True)

    clone_fields = ["remote_url", "secrets_group", "provided_contents"]

    class Meta:
        ordering = ["name"]
        verbose_name = "Git repository"
        verbose_name_plural = "Git repositories"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Store the initial repo slug so we can check for changes on save().
        self.__initial_slug = self.slug

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()

        # Autogenerate slug now, rather than in pre_save(), if not set already, as we need to check it below.
        if self.slug == "":
            self._meta.get_field("slug").create_slug(self, add=(not self.present_in_database))

        if self.present_in_database and self.slug != self.__initial_slug:
            raise ValidationError(
                f"Slug cannot be changed once set. Current slug is {self.__initial_slug}, "
                f"requested slug is {self.slug}"
            )

        if not self.present_in_database:
            check_if_key_is_graphql_safe(self.__class__.__name__, self.slug, "slug")
            # Check on create whether the proposed slug conflicts with a module name already in the Python environment.
            if find_spec(self.slug) is not None:
                raise ValidationError(
                    f'Please choose a different slug, as "{self.slug}" is an installed Python package or module.'
                )

        if self.provided_contents:
            q = models.Q()
            for item in self.provided_contents:
                q |= models.Q(provided_contents__contains=item)
            duplicate_repos = GitRepository.objects.filter(remote_url=self.remote_url).exclude(id=self.id).filter(q)
            if duplicate_repos.exists():
                raise ValidationError(
                    f"Another Git repository already configured for remote URL {self.remote_url} "
                    "provides contents overlapping with this repository."
                )

        # Changing branch or remote_url invalidates current_head
        if self.present_in_database:
            past = GitRepository.objects.get(id=self.id)
            if self.remote_url != past.remote_url or self.branch != past.branch:
                self.current_head = ""

    def get_latest_sync(self):
        """
        Return a `JobResult` for the latest sync operation.

        Returns:
            JobResult
        """
        from nautobot.extras.models import JobResult

        # This will match all "GitRepository" jobs (pull/refresh, dry-run, etc.)
        prefix = "nautobot.core.jobs.GitRepository"
        return JobResult.objects.filter(task_name__startswith=prefix, task_kwargs__repository=self.pk).latest()

    def to_csv(self):
        return (
            self.name,
            self.slug,
            self.remote_url,
            self.branch,
            self.secrets_group.name if self.secrets_group else None,
            self.provided_contents,
        )

    @property
    def filesystem_path(self):
        return os.path.join(settings.GIT_ROOT, self.slug)

    def sync(self, user, dry_run=False):
        """
        Enqueue a Job to pull the Git repository from the remote and return the sync result.

        Args:
            user (User): The User that will perform the sync.
            dry_run (bool): If set, dry-run the Git sync.

        Returns:
            JobResult
        """
        from nautobot.extras.datasources import (
            enqueue_git_repository_diff_origin_and_local,
            enqueue_pull_git_repository_and_refresh_data,
        )

        if dry_run:
            return enqueue_git_repository_diff_origin_and_local(self, user)
        return enqueue_pull_git_repository_and_refresh_data(self, user)

    def clone(self, branch=None, head=None):
        """
        Perform a shallow clone of the Git repository.

        Args:
            branch (str): The branch to checkout. If not set, the GitRepository.branch will be used.
            head (str): Optional Git commit hash to check out instead of pulling branch latest.

        Returns:
            GitRepository
        """
        import copy
        import uuid

        # import tempfile
        from nautobot.core.utils.git import GitRepo
        from nautobot.extras.datasources.git import get_repo_from_url_to_path_and_from_branch

        # tempfile.mkdtemp(dir=self.filesystem_path + "_clone")

        # Shallow copy a GitRepository object with a unique name and slug
        cloned_git_repo = copy.copy(self)
        cloned_git_repo.pk = None
        generated_uuid = uuid.uuid4()

        cloned_git_repo.name = f"{self.name} ({generated_uuid})"
        cloned_git_repo.slug = f"{self.slug}_{generated_uuid}"

        if branch is None and head is None:
            branch = cloned_git_repo.branch
            head = cloned_git_repo.current_head
        elif branch is not None:
            cloned_git_repo.branch = branch
            if head is not None:
                cloned_git_repo.current_head = head
        else:
            cloned_git_repo.save()
            cloned_git_repo.delete()
            raise ValueError("You must provide a branch to checkout a specific commit hash.")

        cloned_git_repo.save()

        # Optionally checkout to a different branch and commit hash
        from_url, to_path, _ = get_repo_from_url_to_path_and_from_branch(cloned_git_repo)
        try:
            repo_helper = GitRepo(to_path, from_url)
            head, changed = repo_helper.checkout(branch, head)
        except Exception:
            cloned_git_repo.delete()
            raise

        return cloned_git_repo
