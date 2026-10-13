import os
import sys
import requests
import logging
from dotenv import load_dotenv
from requests_toolbelt import MultipartEncoder

load_dotenv()

# --- CONFIGURATION ---
HESTIA_API_URL = f"{os.getenv('HESTIA_API_URL', 'http://api.textailes.athenarc.gr')}/reconstructions"
API_KEY = os.getenv('HESTIA_API_KEY')
REQUEST_TIMEOUT = (30, 3600) # read, response

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class LazyFile:

    def __init__(self, path):
        self.path = path
        self.size = os.path.getsize(path)
        self.pos = 0
        self._fh = None

    @property
    def len(self):
        # MultipartEncoder reads `len` as the bytes still left to send
        return self.size - self.pos

    def read(self, size=-1):
        if self._fh is None:
            self._fh = open(self.path, 'rb')
        data = self._fh.read(size)
        self.pos += len(data)
        if self.pos >= self.size:
            self.close()
        return data

    def close(self):
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def upload_reconstruction(scan_id, model_dir):
    if not API_KEY:
        logger.error("API_KEY not found in .env file.")
        return

    if not os.path.exists(model_dir):
        logger.error(f"Model directory {model_dir} does not exist.")
        return

    # 1. Gather relevant files
    # The API expects .obj, .mtl, and texture files (.png, .jpg)
    files_to_upload = [
        f for f in os.listdir(model_dir)
        if f.lower().endswith(('.obj', '.mtl', '.jpg', '.png', '.jpeg'))
    ]

    if not files_to_upload:
        logger.error("No valid 3D model files (obj/mtl/textures) found in directory.")
        return

    logger.info(f"Found {len(files_to_upload)} files to upload for Scan ID: {scan_id}")

    # 2. Prepare Form Data
    # API requires 'scan_id' to link this model to the original images
    fields = [('scan_id', scan_id)]
    lazy_files = []

    try:
        # 3. Prepare Multipart Upload (files are opened only while being sent)
        for filename in files_to_upload:
            file_path = os.path.join(model_dir, filename)

            # Determine content type
            content_type = 'application/octet-stream'
            if filename.lower().endswith('.obj'): content_type = 'text/plain'
            elif filename.lower().endswith('.mtl'): content_type = 'text/plain'
            elif filename.lower().endswith(('.jpg', '.jpeg')): content_type = 'image/jpeg'
            elif filename.lower().endswith('.png'): content_type = 'image/png'

            f = LazyFile(file_path)
            lazy_files.append(f)

            fields.append(('file', (filename, f, content_type)))

        # Streams the body instead of building it in memory
        encoder = MultipartEncoder(fields=fields)

        headers = {
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": encoder.content_type
        }

        # 4. Send Request
        logger.info(f"Uploading {encoder.len / 1024**2:.1f} MiB to {HESTIA_API_URL}...")
        response = requests.post(
            HESTIA_API_URL,
            data=encoder,
            headers=headers,
            timeout=REQUEST_TIMEOUT
        )

        if response.status_code == 201:
            resp_data = response.json()
            logger.info("✅ Upload Successful!")
            logger.info(f"Object ID: {resp_data.get('object_id')}")
            logger.info("The model is now processing (GLB conversion) and will be available to the Annotator.")
        else:
            logger.error(f"❌ Upload Failed. Status: {response.status_code}")
            logger.error(f"Response: {response.text}")

    except Exception as e:
        logger.error(f"An error occurred: {e}")
    finally:
        for f in lazy_files:
            f.close()

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python reconstruction-uploader.py <SCAN_ID> <PATH_TO_MODEL_DIR>")
        sys.exit(1)

    scan_id_arg = sys.argv[1]
    model_dir_arg = sys.argv[2]

    upload_reconstruction(scan_id_arg, model_dir_arg)
