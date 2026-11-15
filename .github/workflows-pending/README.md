# Pending CI workflow

`ci.yml` belongs in `.github/workflows/`. It could not be pushed yet because the GitHub login used for pushing lacks the `workflow` scope. After `gh auth refresh -h github.com -s workflow` it is moved into place.
