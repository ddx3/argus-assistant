"""A permanent signing certificate for the Mac build, so that the microphone and screen-recording
permissions survive updates.

    python deploy/mac/signing_setup.py cert   # create the certificate and key in ~/.argus/ (once)
    uv run --no-project --with pynacl python deploy/mac/signing_setup.py secrets
                                              # put them into GitHub secrets (CI)

Why. macOS permissions (microphone, screen recording) are tied to the program's "code
requirement". With an ad-hoc signature the requirement is the hash of the build itself: every
new build is a different program to the system, so the permissions would be asked for again
after every update (screen recording through "System Settings"). With a permanent
certificate the requirement is "the bundle identifier and this certificate", which does not
change from build to build.

The certificate is self-signed and gives no trust on other people's systems: the first
install from a browser still asks for "Open Anyway"; only an Apple
Developer signature fixes that. Updates that the app installs itself do not get the
quarantine flag and do not raise extra questions. Trust in the update itself rests not on
this certificate but on the release signature (`src/argus/update_trust.py`).

The files live in `~/.argus/` and do not go into the repository: `macos-signing.p12` (the key
with the certificate), `macos-signing.password` (its password), `macos-signing.cer` (the
public part). **Make a copy on reliable media**: if you lose the key, the next build will be
signed by a different certificate and the permissions will have to be granted again.
GitHub secrets: MACOS_CERTIFICATE (the p12 in base64), MACOS_CERTIFICATE_PASSWORD,
MACOS_IDENTITY (the certificate name).
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import secrets as pysecrets
import subprocess
import sys
import urllib.request
from pathlib import Path

HOME = Path.home() / ".argus"
P12 = HOME / "macos-signing.p12"
PASSWORD = HOME / "macos-signing.password"
CERT = HOME / "macos-signing.cer"
IDENTITY = "Argus Release Signing"
REPO = "OWNER/argus"


def make_cert() -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    if P12.exists():
        raise SystemExit(f"{P12} already exists - not overwriting (a different certificate means the permissions would have to be granted again)")
    HOME.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, IDENTITY)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=365 * 20))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=False, data_encipherment=False,
            key_agreement=False, key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False,
        ), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CODE_SIGNING]), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    password = pysecrets.token_urlsafe(24)
    # Old format (3DES + SHA1): macOS `security import` does not always parse the others.
    encryption = (
        serialization.PrivateFormat.PKCS12.encryption_builder()
        .kdf_rounds(50000)
        .key_cert_algorithm(pkcs12.PBES.PBESv1SHA1And3KeyTripleDESCBC)
        .hmac_hash(hashes.SHA1())
        .build(password.encode("ascii"))
    )
    P12.write_bytes(pkcs12.serialize_key_and_certificates(IDENTITY.encode(), key, cert, None, encryption))
    PASSWORD.write_text(password, encoding="ascii")
    CERT.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    for path in (P12, PASSWORD):
        try:
            if os.name == "nt":
                subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{os.environ.get('USERNAME', '')}:F"],
                               capture_output=True, check=False)
            else:
                path.chmod(0o600)
        except OSError:
            pass
    fingerprint = cert.fingerprint(hashes.SHA256()).hex()
    print(f"created: {P12}, {PASSWORD}, {CERT}")
    print(f"certificate fingerprint (SHA-256): {fingerprint}")
    print("MAKE A COPY of these files on a reliable medium: without it a different certificate will sign the next build.")


def github_token() -> str:
    env = dict(os.environ, GCM_INTERACTIVE="never", GIT_TERMINAL_PROMPT="0")
    out = subprocess.run(
        ["git", "credential", "fill"], input=f"protocol=https\nhost=github.com\npath={REPO}.git\n\n",
        capture_output=True, text=True, env=env, timeout=30,
    ).stdout
    for line in out.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no saved GitHub login")


def api(method: str, path: str, token: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        f"https://api.github.com{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "argus-signing-setup"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read()
    return json.loads(data) if data else {}


def put_secrets() -> None:
    from nacl import encoding, public

    for path in (P12, PASSWORD):
        if not path.is_file():
            raise SystemExit(f"no {path}: run `cert` first")
    token = github_token()
    key = api("GET", f"/repos/{REPO}/actions/secrets/public-key", token)
    box = public.SealedBox(public.PublicKey(key["key"].encode(), encoding.Base64Encoder()))
    values = {
        "MACOS_CERTIFICATE": base64.b64encode(P12.read_bytes()).decode("ascii"),
        "MACOS_CERTIFICATE_PASSWORD": PASSWORD.read_text(encoding="ascii").strip(),
        "MACOS_IDENTITY": IDENTITY,
    }
    for name, value in values.items():
        encrypted = base64.b64encode(box.encrypt(value.encode("utf-8"))).decode("ascii")
        api("PUT", f"/repos/{REPO}/actions/secrets/{name}", token, {"encrypted_value": encrypted, "key_id": key["key_id"]})
        print(f"secret saved: {name}")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "cert":
        make_cert()
    elif command == "secrets":
        put_secrets()
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
