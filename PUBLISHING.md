# Publishing GBM to PyPI

The repository includes a manually triggered `.github/workflows/publish.yml` workflow. It tests all supported CI platforms, builds the wheel and source archive, checks package metadata, and then publishes through PyPI Trusted Publishing. No PyPI password or long-lived token is stored in GitHub.

## One-time setup

1. Create or sign in to a https://pypi.org account, verify your email, and enable two-factor authentication.
2. At https://pypi.org/manage/account/publishing/ add a pending GitHub publisher with these exact fields:

   | Field | Value |
   | --- | --- |
   | PyPI project name | `gbm-local` |
   | Owner | `SumanyuDeshpande` |
   | Repository | `GBM` |
   | Workflow filename | `publish.yml` |
   | Environment | `pypi` |

   A pending publisher does not reserve the name; the first successful upload creates the project.
3. In the GitHub repository, open Settings > Environments and create `pypi`. Configure yourself as a required reviewer and restrict deployment to `main`.
4. Push the workflow changes to `main`.

## Publish

In GitHub Actions, select **Publish to PyPI**, choose **Run workflow** on `main`, and approve the `pypi` deployment when prompted. Publication occurs only after the six test jobs and package build succeed. Check the resulting package at https://pypi.org/project/gbm-local/.

Validate in a fresh environment:

```powershell
python -m venv gbm-check
gbm-check\Scripts\python -m pip install --index-url https://pypi.org/simple gbm-local==1.3.0
gbm-check\Scripts\gbm --help
gbm-check\Scripts\gbm undo --help
```

For later releases, increment the version in `pyproject.toml` and `gbm/__init__.py`, push, then run the workflow again. PyPI does not allow reusing an already uploaded distribution filename.

References: https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/ and https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/.
