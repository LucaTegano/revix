from pathlib import Path

from cryptography.hazmat.primitives import serialization

from app.config import settings


def test_key():
    path = Path(settings.GITHUB_PRIVATE_KEY_PATH)
    raw_key = path.read_text()
    private_key_str = raw_key.replace("\\n", "\n").strip('"').strip("'")
    
    try:
        # Try to load the key using cryptography directly
        key = serialization.load_pem_private_key(
            private_key_str.encode('utf-8'),
            password=None
        )
        print("✅ Private key loaded successfully by cryptography.")
        
        # Verify it's an RSA key
        from cryptography.hazmat.primitives.asymmetric import rsa
        if isinstance(key, rsa.RSAPrivateKey):
            print(f"✅ Key is a valid RSA Private Key ({key.key_size} bits).")
        else:
            print(f"❌ Key is NOT an RSA Private Key: {type(key)}")
            
    except Exception as e:
        print(f"❌ Failed to load private key: {e}")

if __name__ == "__main__":
    test_key()
