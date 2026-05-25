# helm integration

This folder wires the repo into helm's per-project **scrapers** cockpit.

- `../helm.json` — the manifest helm reads live (run targets + required secrets).
  helm discovers everything from here, so changing it updates helm with no helm deploy.
- `helm-run.yml` — the GitHub Actions run workflow helm dispatches.
  **It must live at `.github/workflows/helm-run.yml` to work.** It is parked here
  because the token that opened this PR lacks the `workflow` scope and GitHub
  refuses to push workflow files without it. Move it with a `workflow`-scoped
  token (or via the GitHub web UI):

  ```
  mkdir -p .github/workflows && git mv helm/helm-run.yml .github/workflows/helm-run.yml
  ```

- `../helm-run.sh` — the entrypoint the workflow calls. helm stays generic; this
  script (which you can edit from the helm cockpit) defines what each target runs.

Once the workflow is in place and `DEEPSEEK_API_KEY` is pushed from helm
(scrapers → settings → push to repo), "run" in helm dispatches it and the run
shows up in the Runs tab.
