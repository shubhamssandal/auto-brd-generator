import os
import tempfile
import pytest
from coding_agent import _get_bounded_repo_context

def test_bounded_repo_context_security():
    """Verify that sensitive paths are excluded."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create allowed files
        os.makedirs(os.path.join(tmpdir, "src"))
        with open(os.path.join(tmpdir, "src", "main.py"), "w") as f:
            f.write("print('hello')")

        # Create sensitive files/dirs
        os.makedirs(os.path.join(tmpdir, ".ssh"))
        with open(os.path.join(tmpdir, ".ssh", "id_rsa"), "w") as f:
            f.write("secret")
        with open(os.path.join(tmpdir, ".env"), "w") as f:
            f.write("API_KEY=123")
        with open(os.path.join(tmpdir, ".secret_config"), "w") as f:
            f.write("secret")

        context = _get_bounded_repo_context(tmpdir)

        # Should contain src/main.py
        assert "src/main.py" in context

        # Should NOT contain sensitive paths
        assert ".ssh" not in context
        assert "id_rsa" not in context
        assert ".env" not in context
        assert ".secret_config" not in context
