# Homebrew tap

`finances.rb` is the formula for a personal tap, so people can run:

```bash
brew install roshanlam/tap/finances
```

It installs the PyPI wheel into a venv under Homebrew's `python@3.13`, with the
dependencies as prebuilt wheels. The comment at the top of `finances.rb`
explains why it does that instead of building every dependency from source the
way homebrew-core requires.

## One-time: create the tap

Homebrew maps `roshanlam/tap` to the GitHub repo `roshanlam/homebrew-tap`.

```bash
brew tap-new roshanlam/homebrew-tap --no-git     # scaffolds Formula/ and a README locally
#   or create an empty public repo named homebrew-tap on GitHub and clone it
cp packaging/homebrew/finances.rb "$(brew --repository)/Library/Taps/roshanlam/homebrew-tap/Formula/"
cd "$(brew --repository)/Library/Taps/roshanlam/homebrew-tap"
git init && git add . && git commit -m "finances 0.6.0"
git remote add origin git@github.com:roshanlam/homebrew-tap.git
git push -u origin main
```

The repo has to be public and named exactly `homebrew-tap`. The formula goes in
`Formula/finances.rb`.

Check it before pushing:

```bash
brew install --build-from-source roshanlam/tap/finances
brew test finances
brew audit --formula roshanlam/tap/finances      # a few style warnings are expected for a tap
```

## Users

```bash
brew install roshanlam/tap/finances
# same as: brew tap roshanlam/tap && brew install finances

brew upgrade finances
```

## On each release

After the new version is on PyPI, update `url`, `version` and `sha256` in
`Formula/finances.rb` in the tap (and copy it back here so the two stay in step).
The wheel URL contains a hash path, so take it from PyPI rather than guessing:

```bash
V=0.6.0
curl -s "https://pypi.org/pypi/finances/$V/json" | python3 -c '
import json, sys
for f in json.load(sys.stdin)["urls"]:
    if f["filename"].endswith(".whl"):
        print(f["url"]); print(f["digests"]["sha256"])'
```

Paste the first line into `url` and the second into `sha256`, set `version`,
commit with the message `finances <version>`, push, then:

```bash
brew update && brew upgrade finances && brew test finances
```

`brew livecheck roshanlam/tap/finances` reports when PyPI has a newer version
than the formula.

`brew bump-formula-pr` assumes homebrew-core conventions and does not handle
the wheel URL well, so editing the three lines by hand is simpler here.
