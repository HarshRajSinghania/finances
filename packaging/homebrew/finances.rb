# Homebrew formula for the roshanlam/homebrew-tap tap.
#
#   brew install roshanlam/tap/finances
#
# Why this is not a homebrew-core style formula
# ---------------------------------------------
# homebrew-core requires every Python dependency to be a `resource` built from
# source (pip --no-binary :all:). finances has one direct dependency,
# pdfplumber, but that pulls in pypdfium2 (a prebuilt PDFium binary), Pillow
# (libjpeg, zlib, freetype...) and cryptography (a Rust toolchain). Building
# those from source in a tap means depending on rust, jpeg-turbo, freetype and
# friends, a long install, and a formula that breaks whenever any of them moves.
#
# This tap formula instead verifies the finances wheel by sha256, creates a
# venv in libexec with Homebrew's python@3.13, and lets pip install the
# dependencies as the same prebuilt PyPI wheels that `pipx install finances`
# and `uvx finances` use. The tradeoff: dependency versions are resolved at
# install time from PyPI (only `finances` itself is checksummed here), and the
# install needs network access. That is acceptable for a personal tap; it
# would not pass `brew audit --strict` for homebrew-core.
class Finances < Formula
  desc "Local-first personal finance from your own bank statements, checked against their totals"
  homepage "https://github.com/roshanlam/finances"
  url "https://files.pythonhosted.org/packages/bf/0f/28b9a5081e54293cc4caf96fa707d37b81f798dffef275a18378006be3e9/finances-0.4.1-py3-none-any.whl",
      using: :nounzip
  version "0.4.1"
  sha256 "6fd6e6e6430888fe1ce76a6f25d21bc73b009ac13c6598550bde2a60ba0c61a7"
  license "MIT"

  livecheck do
    url "https://pypi.org/pypi/finances/json"
    strategy :json do |json|
      json.dig("info", "version")
    end
  end

  depends_on "python@3.13"

  def python3
    Formula["python@3.13"].opt_bin/"python3.13"
  end

  def install
    wheel = "finances-#{version}-py3-none-any.whl"

    system python3, "-m", "venv", libexec
    system libexec/"bin/python", "-m", "pip", "install", "--quiet",
           "--no-cache-dir", "--disable-pip-version-check",
           # Dependencies come as wheels: no compilers, Rust or image libraries.
           "--only-binary", ":all:",
           buildpath/wheel

    bin.install_symlink libexec/"bin/finances"
  end

  def caveats
    <<~EOS
      Try it on made-up data:
        finances demo && finances web --db demo.db --open

      Your store (finances.db) is created in the directory you run finances
      from, or wherever FINANCES_DB points. Nothing is uploaded anywhere.
    EOS
  end

  test do
    assert_match "finances", shell_output("#{bin}/finances --help")

    # The demo goes through the same reconciliation as a real statement, so a
    # clean `check` proves pdfplumber and the store work, not just the entry point.
    system bin/"finances", "demo", "--db", testpath/"d.db"
    assert_path_exists testpath/"d.db"
    assert_match "Reconciled", shell_output("#{bin}/finances check --db #{testpath}/d.db")
  end
end
