"""Avisos no celular (Web Push) quando um clip fica pronto.

Implementa o necessário do padrão: VAPID (RFC 8292) para identificar o servidor
e criptografia aes128gcm (RFC 8291) da mensagem. As chaves do servidor são
geradas na primeira vez e ficam em WORK_DIR/vapid.json.
"""
import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .. import config
from . import activity

# Só os serviços de push dos navegadores: o servidor não deve postar em URL arbitrária.
PUSH_HOSTS = ("fcm.googleapis.com", ".push.services.mozilla.com", ".push.apple.com",
              ".notify.windows.com")
_lock = threading.Lock()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def _public_bytes(key) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.X962,
                                         serialization.PublicFormat.UncompressedPoint)


def _vapid_key() -> ec.EllipticCurvePrivateKey:
    path = config.WORK_DIR / "vapid.json"
    with _lock:
        try:
            pem = json.loads(path.read_text())["private"]
            return serialization.load_pem_private_key(pem.encode(), password=None)
        except (OSError, ValueError, KeyError):
            key = ec.generate_private_key(ec.SECP256R1())
            pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()).decode()
            config.WORK_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"private": pem}))
            path.chmod(0o600)
            return key


def public_key() -> str:
    """Chave pública do servidor, no formato que o navegador pede ao se inscrever."""
    return _b64(_public_bytes(_vapid_key()))


def allowed_endpoint(endpoint: str) -> bool:
    url = urlparse(endpoint)
    host = url.hostname or ""
    return url.scheme == "https" and any(
        host == h or (h.startswith(".") and host.endswith(h)) for h in PUSH_HOSTS)


def encrypt(payload: bytes, p256dh: bytes, auth: bytes) -> bytes:
    """Corpo aes128gcm (RFC 8291) que só o navegador inscrito consegue abrir."""
    ephemeral = ec.generate_private_key(ec.SECP256R1())
    client = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), p256dh)
    shared = ephemeral.exchange(ec.ECDH(), client)
    server_pub = _public_bytes(ephemeral)
    ikm = _hkdf(auth, shared, b"WebPush: info\x00" + p256dh + server_pub, 32)
    salt = os.urandom(16)
    key = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    ciphertext = AESGCM(key).encrypt(nonce, payload + b"\x02", None)
    return salt + (4096).to_bytes(4, "big") + bytes([len(server_pub)]) + server_pub + ciphertext


def vapid_header(endpoint: str, contact: str) -> str:
    key = _vapid_key()
    origin = "{0.scheme}://{0.netloc}".format(urlparse(endpoint))
    head = _b64(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
    claims = _b64(json.dumps({"aud": origin, "exp": int(time.time()) + 12 * 3600,
                              "sub": contact}).encode())
    r, s = decode_dss_signature(key.sign(f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256())))
    signature = _b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={head}.{claims}.{signature}, k={_b64(_public_bytes(key))}"


# ----- inscrições -----

def _file():
    return config.WORK_DIR / "push.json"


def subscriptions() -> list[dict]:
    try:
        return json.loads(_file().read_text())
    except (OSError, ValueError):
        return []


def _save(subs: list[dict]) -> None:
    config.WORK_DIR.mkdir(parents=True, exist_ok=True)
    _file().write_text(json.dumps(subs[-50:]))


def subscribe(endpoint: str, p256dh: str, auth: str, contact: str) -> None:
    with _lock:
        subs = [s for s in subscriptions() if s["endpoint"] != endpoint]
        _save(subs + [{"endpoint": endpoint, "p256dh": p256dh, "auth": auth, "contact": contact}])


def unsubscribe(endpoint: str) -> None:
    with _lock:
        _save([s for s in subscriptions() if s["endpoint"] != endpoint])


def _send_one(sub: dict, payload: bytes) -> int:
    body = encrypt(payload, _unb64(sub["p256dh"]), _unb64(sub["auth"]))
    request = urllib.request.Request(sub["endpoint"], data=body, method="POST", headers={
        "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
        "TTL": "600", "Urgency": "normal",
        "Authorization": vapid_header(sub["endpoint"], sub.get("contact") or "mailto:admin@localhost"),
    })
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except OSError:
        return 0


def notify(title: str, body: str, url: str = "/#clips") -> None:
    """Manda o aviso para todos os aparelhos inscritos, sem travar quem chamou."""
    subs = subscriptions()
    if not subs:
        return

    def work() -> None:
        payload = json.dumps({"title": title, "body": body, "url": url}).encode()
        for sub in subs:
            code = _send_one(sub, payload)
            if code in (404, 410):          # inscrição expirou ou foi revogada
                unsubscribe(sub["endpoint"])
                activity.log("Um aparelho deixou de receber avisos (a inscrição venceu); ative de novo nele", "warn")
            elif code not in (200, 201, 202):
                activity.log(f"Aviso no celular não foi entregue (resposta {code or 'sem conexão'})", "warn")

    threading.Thread(target=work, daemon=True).start()
