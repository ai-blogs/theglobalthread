import os
import random
import time
import requests
import google.generativeai as genai
from google.api_core import exceptions
from dotenv import load_dotenv
import json

# --- IMPORTS FOR OAuth CLIENT ID (USER CREDENTIALS) ---
from google.oauth2.credentials import Credentials as UserCredentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
# --- END OAuth IMPORTS ---

# Original imports for Google Blogger API (build, HttpError)
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import re
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont, ImageEnhance, ImageFilter
import base64
import logging
from datetime import datetime
import unicodedata
import platform
import html

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('blog_creation.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# IMPORTANT: How to run this script:
# 1. Ensure you have a .env file with GNEWS_API_KEY and GEMINI_API_KEY (for local testing).
# 2. Before running main.py for the first time, run generate_token.py (from Part 2) to create token_blogger.json.
# 3. Install required libraries: pip install python-dotenv requests Pillow google-generativeai google-api-python-client google-auth-httplib2 google-auth-oauthlib
# 4. Run from terminal: python your_script_name.py

# Load environment variables (for local testing)
load_dotenv()

# CONFIGURATION
GNEWS_API_KEY = os.getenv('GNEWS_API_KEY')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
CATEGORIES = ["technology", "health", "sports", "business", "entertainment"]
NUM_SOURCE_ARTICLES_TO_AGGREGATE = 5
LANGUAGE = 'en'
BRANDING_LOGO_PATH = os.getenv('BRANDING_LOGO_PATH', None)
IMAGE_OUTPUT_FOLDER = "transformed_images"
BLOG_OUTPUT_FOLDER = "blog_drafts"

def get_positive_int_env(var_name, default_value):
    """Read positive integer from environment; fallback to default on invalid input."""
    raw_value = os.getenv(var_name, "")
    if not raw_value:
        return default_value

    try:
        value = int(raw_value)
        if value <= 0:
            raise ValueError
        return value
    except ValueError:
        logger.warning(f"Invalid {var_name}='{raw_value}'. Falling back to {default_value}.")
        return default_value

MAX_CATEGORIES_PER_RUN = get_positive_int_env("MAX_CATEGORIES_PER_RUN", 1)

# --- BLOGGER AUTHENTICATION CONFIGURATION ---
BLOGGER_BLOG_ID = os.getenv('BLOGGER_BLOG_ID', '2033568692716180894') # Apni blog ID yahan daalo, ya .env mein.
BLOGGER_POST_STATUS = os.getenv('BLOGGER_POST_STATUS', 'DRAFT').upper()
if BLOGGER_POST_STATUS not in {'LIVE', 'DRAFT'}:
    logger.warning(
        f"Invalid BLOGGER_POST_STATUS='{BLOGGER_POST_STATUS}'. Falling back to 'DRAFT'."
    )
    BLOGGER_POST_STATUS = 'DRAFT'

# These will hold the JSON strings directly from GitHub Secrets OR be read from local files
# In GitHub Actions, these will be populated from secrets.
# Locally, if these env vars are not set, the get_blogger_oauth_credentials() function will
# look for client_secrets.json and token_blogger.json files.
GOOGLE_CLIENT_SECRETS_JSON = os.getenv('GOOGLE_CLIENT_SECRETS_JSON') # Yeh GitHub Secret se aayega
GOOGLE_OAUTH_TOKEN_JSON = os.getenv('GOOGLE_OAUTH_TOKEN_JSON')       # Yeh GitHub Secret se aayega

# Define Blogger Scopes
BLOGGER_SCOPES = ['https://www.googleapis.com/auth/blogger']

# --- LLM Retry Configuration ---
LLM_MAX_RETRIES = 5
LLM_INITIAL_RETRY_DELAY_SECONDS = 5

# --- Enhanced font configuration with fallbacks ---
FONT_PATHS = {
    'mac': [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/SFProText-Regular.ttf"
    ],
    'windows': [
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/calibri.ttf",
        "C:/Windows/Fonts/segoeui.ttf"
    ],
    'linux': [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/TTF/arial.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"
    ]
}

def find_system_font():
    """Find the best available font for the current system"""
    system = platform.system().lower()

    if 'darwin' in system:
        font_list = FONT_PATHS['mac']
    elif 'windows' in system:
        font_list = FONT_PATHS['windows']
    else: # Assume Linux/Unix-like
        font_list = FONT_PATHS['linux']

    for font_path in font_list:
        if os.path.exists(font_path):
            logger.info(f"Using font: {font_path}")
            return font_path

    logger.warning("No suitable system fonts found, using PIL default. Text quality on images may be low.")
    return None

DEFAULT_FONT_PATH = find_system_font()

# Create necessary directories
for folder in [IMAGE_OUTPUT_FOLDER, BLOG_OUTPUT_FOLDER]:
    os.makedirs(folder, exist_ok=True)
    for cat in CATEGORIES:
        os.makedirs(os.path.join(folder, cat), exist_ok=True)

# Setup Gemini API
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    RESEARCH_MODEL = genai.GenerativeModel('models/gemini-2.5-flash-preview-05-20')
    CONTENT_MODEL = genai.GenerativeModel('models/gemini-2.5-flash-preview-05-20')
else:
    logger.error("GEMINI_API_KEY not set. Gemini functions will not work.")

# --- NEW/MODIFIED FUNCTION FOR OAuth CLIENT ID AUTHENTICATION ---
def get_blogger_oauth_credentials():
    """
    Obtains OAuth 2.0 credentials for Blogger API using client_secrets.json and token_blogger.json.
    Prioritizes environment variables (for CI/CD) then local files (for development).
    """
    creds = None
    CLIENT_SECRETS_FILE = 'client_secrets.json' # Local file name (for local testing)
    TOKEN_FILE = 'token_blogger.json' # Local file name (for local testing)

    # 1. Sabse pehle GitHub Secrets (Environment Variables) se load karne ki koshish karo
    if GOOGLE_OAUTH_TOKEN_JSON:
        try:
            token_info = json.loads(GOOGLE_OAUTH_TOKEN_JSON)
            creds = UserCredentials.from_authorized_user_info(token_info, BLOGGER_SCOPES)
            logger.info("INFO: Blogger OAuth token loaded from environment variable (GitHub Secret).")
        except json.JSONDecodeError as e:
            logger.error(f"ERROR: GOOGLE_OAUTH_TOKEN_JSON is not valid JSON: {e}")
            return None
        except Exception as e:
            logger.error(f"ERROR: Could not load Blogger OAuth token from env var: {e}")
            return None

    # Agar credentials valid hain lekin expired hain, toh refresh karne ki koshish karo
    if creds and creds.expired and creds.refresh_token:
        logger.info("INFO: Blogger OAuth token expired, attempting to refresh...")
        try:
            creds.refresh(Request())
            logger.info("INFO: Blogger OAuth token refreshed successfully.")
            # Important: If refreshed, the token_info might change (new expiry).
            # If running locally (not CI), you might want to save it back.
            if not os.getenv('CI'): # Check if not running in CI (e.g., GitHub Actions)
                with open(TOKEN_FILE, 'w') as token_file:
                    token_file.write(creds.to_json())
                logger.info(f"INFO: Refreshed Blogger OAuth token saved to '{TOKEN_FILE}'.")
        except Exception as e:
            logger.error(f"ERROR: Failed to refresh Blogger OAuth token: {e}. You might need to re-authenticate manually by deleting {TOKEN_FILE}.")
            creds = None

    # Agar koi valid credentials environment variable se nahi mile (ya refresh fail hua),
    # aur hum GitHub Actions (CI environment) mein nahi hain, toh local files aur interactive flow try karo.
    # 'CI' env var is usually 'true' in GitHub Actions. So, this block will be skipped in Actions.
    if not creds and not os.getenv('CI'):
        # Local token file se load karne ki koshish karo
        if os.path.exists(TOKEN_FILE):
            try:
                creds = UserCredentials.from_authorized_user_file(TOKEN_FILE, BLOGGER_SCOPES)
                logger.info(f"INFO: Blogger OAuth token loaded from local file '{TOKEN_FILE}'.")
            except Exception as e:
                logger.warning(f"WARNING: Could not load Blogger OAuth token from local file '{TOKEN_FILE}': {e}. Will re-authenticate.")
                creds = None

        # Agar ab bhi credentials nahi mile, toh local interactive OAuth flow initiate karo
        if not creds:
            # Client secrets ko environment variable se load karne ki koshish karo (agar .env mein set hain)
            # ya phir local client_secrets.json file se load karo.
            client_config_info = {}
            if GOOGLE_CLIENT_SECRETS_JSON: # Agar GOOGLE_CLIENT_SECRETS_JSON .env mein set hai
                try:
                    client_config_info = json.loads(GOOGLE_CLIENT_SECRETS_JSON)
                    logger.info("INFO: Client secrets loaded from environment variable (local .env).")
                except json.JSONDecodeError as e:
                    logger.critical(f"CRITICAL ERROR: GOOGLE_CLIENT_SECRETS_JSON in env is not valid JSON: {e}")
                    return None
            elif os.path.exists(CLIENT_SECRETS_FILE): # Agar .env mein nahi hai, toh local file try karo
                try:
                    with open(CLIENT_SECRETS_FILE, 'r') as f:
                        client_config_info = json.load(f)
                    logger.info(f"INFO: Client secrets loaded from local file '{CLIENT_SECRETS_FILE}'.")
                except Exception as e:
                    logger.critical(f"CRITICAL ERROR: Could not load client secrets from '{CLIENT_SECRETS_FILE}': {e}")
                    return None
            else:
                logger.critical(f"CRITICAL ERROR: No client secrets found (neither in GOOGLE_CLIENT_SECRETS_JSON env nor local '{CLIENT_SECRETS_FILE}'). Cannot perform OAuth flow.")
                return None


            logger.info(f"INFO: Initiating interactive OAuth flow for Blogger. Please follow browser instructions.")
            flow = InstalledAppFlow.from_client_config(client_config_info, BLOGGER_SCOPES)
            try:
                # Yeh browser kholega user interaction ke liye (GitHub Actions mein nahi chalega)
                creds = flow.run_local_server(port=0, prompt='consent', authorization_prompt_message='Please authorize this application to access your Blogger account.')
                with open(TOKEN_FILE, 'w') as token_file:
                    token_file.write(creds.to_json())
                logger.info(f"INFO: New token saved to '{TOKEN_FILE}'.")
            except Exception as e:
                logger.error(f"ERROR during local OAuth flow: {e}")
                return None

    if creds and creds.valid:
        logger.info("INFO: Valid Blogger OAuth credentials obtained successfully.")
        return creds

    logger.error("ERROR: Could not obtain valid Blogger OAuth credentials. Posting to Blogger will fail.")
    return None

def validate_environment():
    """Validate that all required environment variables and dependencies are set"""
    errors = []

    if not GNEWS_API_KEY:
        errors.append("GNEWS_API_KEY not found in environment variables.")

    if not GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY not found in environment variables. AI generation will be skipped.")

    # Validate Blogger API credentials
    if not BLOGGER_BLOG_ID:
        errors.append("BLOGGER_BLOG_ID not set. Cannot post to Blogger.")

    try:
        import PIL
        import google.generativeai
        from google.api_core import exceptions
        import requests
        import googleapiclient.discovery
        # OAuth authentication ke liye zaroori imports check karein
        import google_auth_oauthlib.flow
        import google.oauth2.credentials
    except ImportError as e:
        errors.append(f"Missing required package: {e}. Please run 'pip install python-dotenv requests Pillow google-generativeai google-api-python-client google-auth-httplib2 google-auth-oauthlib'.")

    if errors:
        for error in errors:
            logger.error(error)
        return False

    logger.info("Environment validation passed.")
    return True

def sanitize_filename(filename):
    """Create a safe filename from any string"""
    # Normalize unicode characters to their closest ASCII equivalents
    filename = unicodedata.normalize('NFKD', filename).encode('ascii', 'ignore').decode('ascii')
    # Replace invalid characters with underscore, then strip extra underscores/dashes at ends
    safe_title = "".join(c if c.isalnum() or c in (' ', '-', '_') else '_' for c in filename).strip()
    safe_title = re.sub(r'[_ -]+', '_', safe_title).lower() # Ensure single underscores and lowercase
    return safe_title[:100] # Truncate to a reasonable length for file paths

def parse_metadata_list_field(raw_value):
    """
    Parse metadata list fields that are expected in format: [value1, value2]
    """
    if not raw_value:
        return []

    value = raw_value.strip()
    if value.startswith('[') and value.endswith(']'):
        value = value[1:-1]

    items = []
    for item in value.split(','):
        cleaned = item.strip().strip('"').strip("'")
        if cleaned:
            items.append(cleaned)

    return items

def sanitize_external_url(url, allow_data=False):
    """
    Allow only safe URL schemes for generated HTML links and images.
    """
    if not url:
        return ''

    cleaned = url.strip()
    if re.match(r'^https?://', cleaned, re.IGNORECASE):
        return cleaned
    if allow_data and cleaned.startswith('data:image/'):
        return cleaned
    if re.match(r'^mailto:', cleaned, re.IGNORECASE):
        return cleaned

    logger.warning(f"Blocked potentially unsafe URL scheme: {cleaned[:80]}")
    return '#'

def fetch_gnews_articles(category, max_articles_to_fetch=10, max_retries=3):
    """Fetches articles from GNews API with retry logic"""
    url = f'https://gnews.io/api/v4/top-headlines'
    params = {
        'category': category,
        'lang': LANGUAGE,
        'token': GNEWS_API_KEY,
        'max': max_articles_to_fetch # Request up to this many articles
    }

    for attempt in range(max_retries):
        try:
            logger.info(f"Fetching up to {max_articles_to_fetch} articles for {category} (attempt {attempt + 1})...")
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)

            data = resp.json()
            articles = data.get('articles', [])

            if not articles:
                logger.warning(f"No articles found for category {category} from GNews API.")
                return []

            # Select unique articles based on URL to avoid duplicates if API sends similar ones
            unique_articles = {article['url']: article for article in articles}.values()

            selected_articles = list(unique_articles)[:max_articles_to_fetch] # Cap at requested max
            logger.info(f"Successfully fetched {len(selected_articles)} articles for {category}.")
            return selected_articles

        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching articles for {category} (attempt {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)  # Exponential backoff
            else:
                logger.error(f"Failed to fetch articles for {category} after {max_retries} attempts")
                return []
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON response from GNews API: {e}")
            return []

def aggregate_articles(articles_list, category):
    """
    Aggregates data from multiple articles to create a consolidated view
    for a single, unique blog post.
    """
    if not articles_list:
        logger.warning(f"No articles provided for aggregation in category {category}.")
        return None

    consolidated_content = []
    consolidated_descriptions = []
    titles = []
    image_url = None
    competitor_domains = set()
    primary_source_url_for_disclaimer = None # To link back to one source in the disclaimer

    # Strategy for image & primary source:
    # 1. Sort articles by content length (descending) to prioritize substantive ones.
    # 2. Iterate and pick the first valid image from a reasonably long article.
    # 3. If no image found from long articles, take the first available image.
    image_found = False

    # Sort articles by content length to prioritize more substantive sources for aggregation
    sorted_articles = sorted(articles_list, key=lambda x: len(x.get('content', '')), reverse=True)

    for i, article in enumerate(sorted_articles):
        title = article.get('title', '')
        description = article.get('description', '')
        content = article.get('content', '')
        source_url = article.get('url', '')
        article_image = article.get('image', '')
        source_domain = article.get('source', {}).get('url', '').replace('https://', '').replace('http://', '').split('/')[0]

        if title: titles.append(title)
        if description: consolidated_descriptions.append(description)

        # Only add content if it's substantial and not a placeholder "[Removed]"
        if content and content.strip() != '[Removed]' and len(content.strip()) > 50:
            # Append content with a clear source identifier for the AI
            consolidated_content.append(f"### Source: {title}\n\n{content}")

            # If we haven't found an image yet, and this article has one and decent content length, take it.
            if not image_found and article_image and len(content) > 100:
                image_url = article_image
                primary_source_url_for_disclaimer = source_url
                image_found = True

        if source_domain:
            competitor_domains.add(source_domain)

    # Fallback for image: if no image found from content-rich articles, take the first one available in the original list
    if not image_url:
        for article in articles_list: # Loop through original order for simpler first-available
            if article.get('image'):
                image_url = article['image']
                primary_source_url_for_disclaimer = article['url']
                break

    if not image_url:
        logger.warning(f"No valid image URL found among aggregated articles for category {category}. Proceeding without image.")

    # Formulate a consolidated topic based on primary titles
    # This will be further refined by the research agent.
    consolidated_topic = titles[0] if titles else f"Recent Developments in {category.capitalize()}"
    if len(titles) > 1:
        # Simple heuristic: combine a few of the top titles
        combined_titles_string = " ".join(titles[:min(3, len(titles))])
        consolidated_topic = f"Comprehensive Look: {combined_titles_string}"
        if len(consolidated_topic) > 150: # Truncate if too long
            consolidated_topic = consolidated_topic[:150] + "..."
        consolidated_topic = consolidated_topic.replace('...', '...').strip() # Clean up dots

    # Use a dummy description if none are available
    if not consolidated_descriptions:
        consolidated_descriptions.append(f"A deep dive into recent developments in {category}.")

    return {
        "consolidated_topic": consolidated_topic,
        "combined_content": "\n\n---\n\n".join(consolidated_content) if consolidated_content else "No substantial content found from sources. AI will generate based on topic.",
        "combined_description": " ".join(consolidated_descriptions)[:300].strip(), # Keep description concise
        "image_url": image_url,
        "competitors": list(competitor_domains),
        "primary_source_url": primary_source_url_for_disclaimer if primary_source_url_for_disclaimer else articles_list[0]['url'] if articles_list else 'https://news.example.com/source-unavailable'
    }

def enhance_image_quality(img):
    """Apply advanced image enhancement techniques."""
    # Convert to RGB if not already, to ensure consistent processing for JPEG saving
    if img.mode != 'RGB':
        img = img.convert('RGB')

    # Apply subtle enhancements
    enhancer = ImageEnhance.Sharpness(img)
    img = enhancer.enhance(1.2) # Sharpen slightly

    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(1.1) # Increase contrast slightly

    enhancer = ImageEnhance.Color(img)
    img = enhancer.enhance(1.05) # Boost color saturation a bit

    # Apply a subtle unsharp mask effect for perceived sharpness
    img = img.filter(ImageFilter.UnsharpMask(radius=1.5, percent=150, threshold=3))

    return img

def create_text_with_shadow(draw, position, text, font, text_color, shadow_color, shadow_offset):
    """Draw text with shadow for better visibility."""
    x, y = position
    shadow_x, shadow_y = shadow_offset

    # Draw shadow
    draw.text((x + shadow_x, y + shadow_y), text, font=font, fill=shadow_color)
    # Draw main text
    draw.text((x, y), text, font=font, fill=text_color)

def find_content_bbox_and_trim(img, tolerance=20, border_colors_to_trim=((0,0,0), (255,255,255))):
    """
    Attempts to find the bounding box of non-border content pixels and trims the image.
    Considers black and white as potential uniform border colors.
    Increased tolerance for slight variations in border color.
    """
    if img.mode != 'RGB':
        img = img.convert('RGB')

    width, height = img.size
    pixels = img.load() # Get pixel access for speed

    def is_similar(pixel1, pixel2, tol):
        """Checks if two pixels are similar within a given tolerance."""
        return all(abs(c1 - c2) <= tol for c1, c2 in zip(pixel1, pixel2))

    def is_border_pixel_group(pixel):
        """Checks if a pixel is similar to any of the specified border colors."""
        return any(is_similar(pixel, bc, tolerance) for bc in border_colors_to_trim)

    # Find top
    top = 0
    for y in range(height):
        if not all(is_border_pixel_group(pixels[x, y]) for x in range(width)):
            top = y
            break

    # Find bottom
    bottom = height
    for y in range(height - 1, top, -1):
        if not all(is_border_pixel_group(pixels[x, y]) for x in range(width)):
            bottom = y + 1
            break

    # Find left
    left = 0
    for x in range(width):
        if not all(is_border_pixel_group(pixels[x, y]) for y in range(height)):
            left = x
            break

    # Find right
    right = width
    for x in range(width - 1, left, -1):
        if not all(is_border_pixel_group(pixels[x, y]) for y in range(height)):
            right = x + 1
            break

    if (left, top, right, bottom) != (0, 0, width, height):
        # Ensure that after trimming, a reasonable amount of content remains
        # and that trimming isn't just cropping out actual image content mistakenly
        # This prevents aggressive trimming of images that naturally have large single-color areas
        min_content_ratio = 0.75 # Ensure at least 75% of original width/height remains after trim
        trimmed_width = right - left
        trimmed_height = bottom - top
        if trimmed_width > (width * min_content_ratio) and \
           trimmed_height > (height * min_content_ratio):
            logger.info(f"Automatically trimmed detected uniform borders from original image. BBox: ({left}, {top}, {right}, {bottom})")
            return img.crop((left, top, right, bottom))
        else:
            logger.debug(f"Trimming borders would remove too much content ({trimmed_width}/{width} or {trimmed_height}/{height}). Skipping trim.")

    logger.debug("No significant uniform color borders detected in original image for trimming.")
    return img


def transform_image(image_url, title_text, category_text, output_category_folder, safe_filename):
    """
    Downloads, processes, and adds branding/text to an image.
    Saves the image to disk and returns its file path and Base64 encoded string.
    Returns (relative_file_path, base64_data_uri) or (None, None) on failure.
    """
    if not image_url:
        logger.info("No image URL provided for transformation. Skipping image processing.")
        return None, None

    output_full_path = None
    base64_data_uri = None

    try:
        logger.info(f"Processing image from URL: {image_url[:70]}...")

        # Download image with proper headers to avoid 403 Forbidden errors
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(image_url, timeout=20, stream=True, headers=headers)
        response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)

        # Load image, handling potential errors and different modes
        img = Image.open(BytesIO(response.content))

        # Convert to RGB for consistent processing and JPEG saving. If there's an alpha channel,
        # create a white background, otherwise just convert.
        if img.mode in ('RGBA', 'LA', 'P'):
            alpha = img.split()[-1] if img.mode in ('RGBA', 'LA') else None
            background = Image.new('RGB', img.size, (255, 255, 255))
            if alpha: # Paste with alpha mask
                background.paste(img, mask=alpha)
            else: # Just paste if no alpha or if 'P' mode without alpha
                background.paste(img)
            img = background
        elif img.mode != 'RGB':
            img = img.convert('RGB')

        # --- STEP 1: Automatically trim uniform borders (letterboxing/pillarboxing) from the source image ---
        # This will return the original image if no significant borders are found.
        img = find_content_bbox_and_trim(img)

        # Target dimensions for the *visual content* of the blog featured image (16:9 aspect ratio)
        target_content_width = 1200
        target_content_height = 675
        target_aspect = target_content_width / target_content_height

        # --- STEP 2: Resize and Crop to fill 16:9 (Object-Fit: Cover equivalent) ---
        # This ensures the core visual area is always 1200x675
        original_width, original_height = img.size
        original_aspect = original_width / original_height

        if original_aspect > target_aspect: # Original image is wider than target aspect ratio
            # Scale height to match target_content_height, then crop width
            resize_height = target_content_height
            resize_width = int(target_content_height * original_aspect)
        else: # Original image is taller than target aspect ratio
            # Scale width to match target_content_width, then crop height
            resize_width = target_content_width
            resize_height = int(target_content_width / original_aspect)

        # Resize the image so that it completely covers the target content dimensions
        img = img.resize((resize_width, resize_height), Image.Resampling.LANCZOS)

        # Center crop to the exact target content dimensions (removes excess equally from sides)
        left_crop = (resize_width - target_content_width) // 2
        top_crop = (resize_height - target_content_height) // 2
        img = img.crop((left_crop, top_crop, left_crop + target_content_width, top_crop + target_content_height))

        # --- STEP 3: Apply image enhancements to the 16:9 content ---
        img = enhance_image_quality(img)

        # Convert to RGBA for drawing elements
        img = img.convert('RGBA')

        # --- STEP 4: Create a clean, dynamically-extended bottom area for text with gradient ---
        # This creates the "title card" effect.

        # Define the height of the new extended area for text (e.g., 25% of the 16:9 content height)
        extended_area_height = int(target_content_height * 0.25)

        # Calculate the total height of the final image canvas
        final_canvas_height = target_content_height + extended_area_height
        final_canvas_width = target_content_width # Same width as target_content_width

        # Create the new larger canvas for the final image. Initialize with the cropped image.
        # This canvas will hold the 16:9 image on top and the extended part below.
        new_combined_img = Image.new('RGBA', (final_canvas_width, final_canvas_height), (0, 0, 0, 255)) # Start with opaque black background
        new_combined_img.paste(img, (0, 0)) # Paste the 16:9 content image at the top

        # Dynamically extend the bottom few rows of the 16:9 image downwards
        # This creates a seamless visual continuation from the main image content into the extended area.
        strip_from_original_height = int(target_content_height * 0.05) # Sample a 5% strip from the bottom of the 16:9 content
        if strip_from_original_height > 0:
            bottom_strip_for_extension = img.crop((0, target_content_height - strip_from_original_height, target_content_width, target_content_height))
            stretched_strip = bottom_strip_for_extension.resize((target_content_width, extended_area_height), Image.Resampling.BICUBIC)
            new_combined_img.paste(stretched_strip, (0, target_content_height)) # Paste this stretched strip into the extended area
            logger.info("Extended bottom of image with stretched content for seamless look.")

        # Create a new, transparent image specifically for the gradient overlay.
        # This image must be the full size of `new_combined_img` so it can be alpha_composited correctly.
        gradient_overlay_image = Image.new('RGBA', new_combined_img.size, (0, 0, 0, 0))
        draw_gradient = ImageDraw.Draw(gradient_overlay_image)

        # Draw the gradient lines only within the extended area on this new gradient image.
        # The gradient fades from mostly transparent to mostly opaque black.
        gradient_top_y_on_canvas = target_content_height # Absolute Y where gradient starts on the final canvas
        for y_relative_to_extended_area in range(extended_area_height):
            # Alpha increases from 0 (transparent) at the top of the extended area
            # to 95% opaque black at the bottom of the extended area.
            alpha = int(255 * (y_relative_to_extended_area / extended_area_height) * 0.95)
            absolute_y_on_canvas = gradient_top_y_on_canvas + y_relative_to_extended_area
            draw_gradient.line([(0, absolute_y_on_canvas), (final_canvas_width, absolute_y_on_canvas)], fill=(0, 0, 0, alpha))

        # Composite the gradient overlay image onto the `new_combined_img`.
        # This is the correct way to blend an RGBA image (gradient) over another RGBA image (new_combined_img).
        img = Image.alpha_composite(new_combined_img, gradient_overlay_image)

        # Re-initialize draw object for the `img` (which is now the final canvas with content and gradient)
        draw = ImageDraw.Draw(img)


        # --- STEP 5: Add Custom Branding (Logo) - Positioned in TOP-RIGHT ---
        if BRANDING_LOGO_PATH and os.path.exists(BRANDING_LOGO_PATH):
            try:
                logo = Image.open(BRANDING_LOGO_PATH).convert("RGBA")
                logo_height = int(target_content_height * 0.08) # Logo height is 8% of image height
                logo_width = int(logo.width * (logo_height / logo.height)) # Maintain logo aspect ratio
                logo = logo.resize((logo_width, logo_height), Image.Resampling.LANCZOS)

                # Position logo in top right with padding relative to the original 16:9 content area
                padding = int(target_content_width * 0.02) # 2% padding from edges
                logo_x = target_content_width - logo_width - padding
                logo_y = padding

                # Create a temporary transparent overlay for the logo and alpha-composite it.
                # This ensures the logo's transparency is handled correctly over the image.
                logo_overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
                logo_overlay.paste(logo, (logo_x, logo_y), logo) # Use logo itself as mask for transparency
                img = Image.alpha_composite(img, logo_overlay)
                logger.info("Branding logo applied successfully.")
            except Exception as e:
                logger.error(f"Error applying branding logo: {e}")

        # --- STEP 6: Add Title Text (bottom right corner, on extended area) ---
        # Font setup: Ensure selected_font is defined before use
        selected_font = ImageFont.load_default() # Initialize with a default font
        title_font_size = max(int(target_content_height * 0.035), 20) # Ensure a minimum readable size

        # Attempt to load custom font, fallback to default if error
        if DEFAULT_FONT_PATH:
            try:
                selected_font = ImageFont.truetype(DEFAULT_FONT_PATH, title_font_size)
            except (IOError, OSError) as e:
                logger.warning(f"Could not load specified font '{DEFAULT_FONT_PATH}': {e}. Falling back to PIL default.")
                # `selected_font` remains `ImageFont.load_default()` if custom fails
        else:
            logger.warning("No default font path specified. Using PIL default font. Text quality on images may be basic.")

        # Re-initialize draw object for the final `img` after all compositing steps for text drawing
        draw = ImageDraw.Draw(img)

        max_text_width_for_title = int(target_content_width * 0.45) # Title text can take up to 45% of image width
        horizontal_padding_text = int(target_content_width * 0.02) # 2% padding from edges for text

        def get_wrapped_text_lines(text, font, max_width):
            """Wraps text to fit within a given maximum width."""
            lines = []
            if not text: return lines
            words = text.split()
            if not words: return lines

            current_line = words[0]
            for word in words[1:]:
                test_line = f"{current_line} {word}".strip()
                try: # Use getbbox for accurate truetype font measurements
                    bbox = font.getbbox(test_line)
                    text_width = bbox[2] - bbox[0]
                except AttributeError: # Fallback for load_default font
                    text_width = font.getsize(test_line)[0]

                if text_width <= max_width:
                    current_line = test_line
                else:
                    lines.append(current_line)
                    current_line = word
            lines.append(current_line)
            return lines

        wrapped_title_lines = get_wrapped_text_lines(title_text, selected_font, max_text_width_for_title)

        # Calculate total height of wrapped text block for vertical positioning
        total_text_height_for_placement = 0
        line_height_list = []
        for line in wrapped_title_lines:
            try:
                bbox = selected_font.getbbox(line)
                line_height = bbox[3] - bbox[1]
                line_height_list.append(line_height + int(title_font_size * 0.2)) # Add small line spacing
            except AttributeError:
                line_height_list.append(title_font_size + 3) # Fallback for default font

        total_text_height_for_placement = sum(line_height_list)

        # Determine starting Y position for the text block (bottom-aligned relative to FINAL canvas height)
        bottom_align_y_coord = final_canvas_height - horizontal_padding_text
        current_y_text_draw = bottom_align_y_coord - total_text_height_for_placement

        # Ensure text is safely within the newly created extended area's boundaries
        # It should start below where the original 16:9 image *ends* on the canvas
        min_y_for_text = target_content_height + horizontal_padding_text
        if current_y_text_draw < min_y_for_text:
            current_y_text_draw = min_y_for_text


        # Draw each line of the title with shadow
        for i, line in enumerate(wrapped_title_lines):
            try:
                bbox = selected_font.getbbox(line)
                line_width = bbox[2] - bbox[0]
            except AttributeError:
                line_width = selected_font.getsize(line)[0]

            x_text_draw = target_content_width - horizontal_padding_text - line_width # Right-align each line

            create_text_with_shadow(
                draw, (x_text_draw, current_y_text_draw), line, selected_font,
                (255, 255, 255, 255), (0, 0, 0, 180), (2, 2) # White text, subtle black shadow
            )
            current_y_text_draw += line_height_list[i] # Move Y down for the next line

        # Save the transformed image to disk as JPEG for web optimization
        # The output image will have the new `final_canvas_height` (e.g., 1200x843.75)
        output_filename = f"{safe_filename}_{int(time.time())}.jpg"
        output_full_path = os.path.join(IMAGE_OUTPUT_FOLDER, output_category_folder, output_filename)

        # Convert to RGB before saving as JPEG (removes alpha channel)
        final_img_for_save = img.convert('RGB')

        # Save to a BytesIO object first to get raw bytes for Base64 encoding
        buffer = BytesIO()
        final_img_for_save.save(buffer, format='JPEG', quality=85, optimize=True) # Optimize for web
        image_bytes = buffer.getvalue()

        # Generate Base64 data URI
        base64_encoded_image = base64.b64encode(image_bytes).decode('utf-8')
        base64_data_uri = f"data:image/jpeg;base64,{base64_encoded_image}"

        # Save to disk
        with open(output_full_path, 'wb') as f:
            f.write(image_bytes)

        logger.info(f"Transformed image saved to disk: {output_full_path}")
        logger.info(f"Transformed image Base64 encoded.")
        return output_full_path, base64_data_uri

    except requests.exceptions.RequestException as e:
        logger.error(f"Error downloading or requesting image {image_url}: {e}")
        return None, None
    except IOError as e: # PIL specific errors (e.g., cannot identify image file)
        logger.error(f"Error processing image file from {image_url}: {e}")
        return None, None
    except Exception as e:
        logger.error(f"Unexpected error during image transformation for {image_url}: {e}", exc_info=True)
        return None, None

def _gemini_generate_content_with_retry(model, prompt, max_retries=LLM_MAX_RETRIES, initial_delay=LLM_INITIAL_RETRY_DELAY_SECONDS):
    """
    Helper function to call Gemini's generate_content with retry logic for transient errors.
    """
    for attempt in range(max_retries):
        try:
            response = model.generate_content(prompt)
            # Check for empty response or issues from the model's side
            if not response.text or response.text.strip() == "":
                logger.warning(f"Attempt {attempt + 1}: Gemini returned empty response. Retrying...")
                raise ValueError("Empty response from Gemini model.")

            return response
        except (
            exceptions.InternalServerError, # Use 'exceptions' imported from google.api_core
            exceptions.ResourceExhausted,   # Use 'exceptions' imported from google.api_core
            exceptions.DeadlineExceeded,    # Handle 504 Deadline Exceeded explicitly
            requests.exceptions.RequestException, # General network errors (e.g., connection reset by peer)
            ValueError # Custom empty response error
        ) as e:
            logger.warning(f"Attempt {attempt + 1} failed with error: {e}")
            if attempt < max_retries - 1:
                sleep_time = initial_delay * (2 ** attempt) + random.uniform(0, 2) # Exponential backoff with jitter
                logger.info(f"Retrying in {sleep_time:.2f} seconds...")
                time.sleep(sleep_time)
            else:
                logger.error(f"Gemini API call failed after {max_retries} attempts.")
                raise # Re-raise the last exception if all retries fail


def perform_research_agent(target_topic, competitors):
    """
    Acts as the 'Research Agent'. Uses Gemini to find SEO keywords and outline suggestions.
    Outputs a JSON string.
    """
    if not RESEARCH_MODEL:
        logger.error("Research model not initialized. Skipping research agent.")
        return None

    prompt = (
        f"You are an expert SEO Keyword Research Agent specializing in market analysis and content strategy. "
        f"Your task is to perform comprehensive SEO keyword research and outline generation for the topic: '{target_topic}'.\n\n"
        f"Analyze content from top competitors (e.g., {', '.join(competitors[:5])}) to identify relevant SEO keywords, content gaps, and structural insights.\n\n"
        f"**Crucial:** Based on the topic, original source information, and keyword research, generate a **unique, catchy, and SEO-optimized blog post title (H1)** that will attract readers and rank well. This title should be distinct from the original source titles and reflect a consolidated, in-depth perspective.\n\n"
        "## Process Flow:\n"
        "1.  **Initial Keyword Discovery:** Identify primary (high search volume, high relevance), secondary (long-tail, specific), and diverse keyword clusters related to the target topic. Think about various user intents (informational, commercial, navigational).\n"
        "2.  **Competitive Analysis:** Provide 2-3 key insights into competitor strategies and content gaps in relation to the topic.\n"
        "3.  **Keyword Evaluation:** Assess search volume and competition levels for identified keywords. Prioritize high-value, relevant keywords for SEO optimization. Identify important related entities and concepts.\n"
        "4.  **Outline Creation:** Generate a detailed, hierarchical blog post outline (using markdown headings `##`, `###`) that strategically incorporates the high-value keywords. Ensure the outline flows logically and covers comprehensive aspects of the topic. Suggest potential sub-sections for FAQs, case studies, or data points where appropriate.\n\n"
        "## Output Specifications:\n"
        "Generate a JSON object (as a string) with the following structure. Ensure the `blog_outline` is a valid markdown string.\n"
        "```json\n"
        "{{\n"
        "  \"suggested_blog_title\": \"Your Unique and Catchy Blog Post Title Here\",\n"
        "  \"primary_keywords\": [\"keyword1\", \"keyword2\", \"keyword3\"],\n"
        "  \"secondary_keywords\": {{\"sub_topic1\": [\"keywordA\", \"keywordB\"], \"sub_topic2\": [\"keywordC\", \"keywordD\"]}},\n"
        "  \"competitor_insights\": \"Summary of competitor strategies and content gaps.\",\n"
        "  \"blog_outline\": \"## Introduction\\n\\n### Hook\\n\\n## Main Section 1: [Section Title]\\n\\n### Sub-section 1.1\\n\\n## Conclusion\\n\"\n"
        "}}\n"
        "```\n"
        "**Constraints:** Focus on commercially relevant terms. Exclude branded competitor terms. The entire output must be a single, valid JSON string. The `blog_outline` must contain at least 8 distinct markdown headings (H2 or H3) and be structured for user engagement and SEO. The `suggested_blog_title` should be concise, impactful, and ideally under 70 characters. Do NOT include any introductory or concluding remarks outside the JSON block."
    )
    try:
        logger.info(f"Generating research for: '{target_topic[:70]}...'")
        # Use the helper function here
        response = _gemini_generate_content_with_retry(RESEARCH_MODEL, prompt)

        # Extract the JSON block from the response text
        json_match = re.search(r'```json\s*(.*?)\s*```', response.text, re.DOTALL)
        if json_match:
            json_str = json_match.group(1).strip()
            research_data = json.loads(json_str)
            logger.info("Research generation successful.")
            return research_data
        else:
            logger.warning(f"Could not find valid JSON in markdown block for '{target_topic}'. Attempting to parse raw response.")
            try:
                # Attempt to parse the entire response if markdown block not found
                research_data = json.loads(response.text.strip())
                logger.info("Research generation successful (parsed raw response).")
                return research_data
            except json.JSONDecodeError:
                logger.error(f"Failed to parse research response as JSON for '{target_topic}'. Raw response:\n{response.text[:500]}...")
                return None

    except (json.JSONDecodeError, ValueError, requests.exceptions.RequestException, exceptions.InternalServerError, exceptions.ResourceExhausted, exceptions.DeadlineExceeded) as e:
        logger.error(f"Research Agent generation failed or JSON parsing/content validation failed for '{target_topic}': {e}.")
        return None
    except Exception as e:
        logger.error(f"Research Agent generation failed for '{target_topic}': {e}", exc_info=True)
        return None

def generate_content_agent(consolidated_article_data, research_output, transformed_image_filepath):
    """
    Acts as the 'Content Generator Agent'. Uses Gemini to write the blog post
    based on aggregated source data and research output.
    """
    if not CONTENT_MODEL:
        logger.error("Content model not initialized. Skipping content generation.")
        return None

    image_path_for_prompt = transformed_image_filepath if transformed_image_filepath else "None"

    # Extract keywords for easier prompting
    primary_keywords_str = ', '.join(research_output.get('primary_keywords', []))
    secondary_keywords_str = ', '.join([kw for sub_list in research_output.get('secondary_keywords', {}).values() for kw in sub_list])

    # Get the new suggested title
    new_blog_title = research_output.get('suggested_blog_title', consolidated_article_data.get('consolidated_topic', 'Default Consolidated Blog Title'))

    # Log the keywords being used
    logger.info(f"Using primary keywords for content generation: {primary_keywords_str}")
    logger.info(f"Using secondary keywords for content generation: {secondary_keywords_str}")

    # Prepare metadata for logging
    category = consolidated_article_data.get('category', 'general')
    primary_keywords = research_output.get('primary_keywords', [])
    secondary_keywords = []
    if research_output.get('secondary_keywords'):
        for sub_list in research_output.get('secondary_keywords', {}).values():
            secondary_keywords.extend(sub_list)
    
    # Log the metadata that will be included
    logger.info("Preparing metadata for blog post:")
    logger.info(f"Title: {new_blog_title}")
    logger.info(f"Category: {category}")
    logger.info(f"Primary Keywords: {primary_keywords}")
    logger.info(f"Secondary Keywords: {secondary_keywords}")

    # TRUNCATE combined_content to avoid hitting context limits
    combined_content_for_prompt = consolidated_article_data.get('combined_content', '')
    if len(combined_content_for_prompt) > 4000:
        combined_content_for_prompt = combined_content_for_prompt[:4000] + "\n\n[...Content truncated for prompt brevity...]"
        logger.info(f"Truncated combined_content for prompt: {len(consolidated_article_data['combined_content'])} -> {len(combined_content_for_prompt)} characters.")

    # Re-dump the consolidated_article_data with truncated content
    consolidated_article_data_for_prompt = consolidated_article_data.copy()
    consolidated_article_data_for_prompt['combined_content'] = combined_content_for_prompt

    # Safely prepare description for embedding in the prompt
    raw_description_for_prompt = consolidated_article_data.get(
        'combined_description',
        'A comprehensive and insightful look at the latest news and trends.'
    )
    blog_description_for_prompt = raw_description_for_prompt.replace('"', '').replace('\n', ' ').replace('\r', ' ').strip()[:155]

    # Construct the prompt
    prompt = (
        f"You are a specialized Blog Writing Agent that transforms SEO research and aggregated article data "
        f"into comprehensive, publication-ready, SEO-optimized blog posts. You excel at creating in-depth, "
        f"authoritative content by synthesizing information from multiple sources, while maintaining reader engagement and SEO best practices.\n\n"
        f"## Input Requirements:\n"
        f"1.  `aggregated_source_data`: {json.dumps(consolidated_article_data_for_prompt, indent=2)}\n"
        f"2.  `research_output`: {json.dumps(research_output, indent=2)}\n"
        f"3.  `transformed_image_path_info`: '{image_path_for_prompt}' (This is the file path to the main featured image. Do NOT embed this image again within the content body. It will be handled separately in the HTML template.)\n\n"
        f"## Content Specifications:\n"
        f"-   **Word Count:** Aim for 2500-3000 words. Synthesize and expand thoughtfully on the `aggregated_source_data['combined_content']`, adding depth and concrete explanations. Do NOT simply copy-paste content from the input. Rewrite and integrate.\n"
        f"-   **Heading Structure:** Use the provided outline (`research_output['blog_outline']`). Ensure a minimum of 25 headings (`##` and `###` only, except for the main H1 title).\n"
        f"-   **Paragraph Length:** Each paragraph should contain at least 5 sentences for comprehensive coverage, unless it's a short intro/outro or a bullet point explanation.\n"
        f"-   **Writing Style:** Professional yet conversational, engaging, and human-like. Avoid jargon where simpler terms suffice. Do NOT mention that you are an AI or generated the content. Ensure a clear, authoritative, and trustworthy tone that positions the content as highly credible.\n"
        f"-   **Target Audience:** Broad audience interested in the specified category.\n"
        f"-   **Keyword Integration:** Naturally weave `primary_keywords` ({primary_keywords_str}) and `secondary_keywords` ({secondary_keywords_str}) throughout the text without keyword stuffing. Integrate them into headings, subheadings, and body paragraphs.\n"
        f"-   **Content Expansion:** Elaborate significantly on the `aggregated_source_data['combined_content']` by adding specific details, explanations, and context, drawing from your extensive knowledge base. Emphasize synthesizing information from *all provided sources* to create a unique and comprehensive article.\n"
        f"-   **Data & Examples:** Incorporate relevant data, statistics, and real-world examples only when they can be grounded in reliable public reporting or the provided source material. Do NOT fabricate facts, numbers, names, dates, studies, or organizations.\n"
        f"-   **Linking:** Include external links only when the URL is directly present in the provided source data or is an official homepage of a known organization. Do NOT invent links, do NOT use placeholder domains, and do NOT add hypothetical internal links.\n"
        f"-   **Image Inclusion:** Do NOT include any markdown `![alt text](image_path)` syntax for the featured image within the generated content body. The featured image is handled separately. Crucially, do NOT generate any `![alt text](image_path)` markdown for additional images within the content body. The single `featuredImage` is handled separately by the HTML template and should not be re-included.\n"
        f"## Output Structure:\n"
        f"Generate the complete blog post in markdown format. It must start with a metadata block followed by the blog content.\n\n"
        f"**Metadata Block (exact key-value pairs, no --- delimiters, newline separated):**\n"
        f"title: {new_blog_title}\n"
        f"description: {blog_description_for_prompt}\n"
        f"date: {datetime.now().strftime('%Y-%m-%d')}\n"
        f"categories: [{category}, {', '.join(primary_keywords[:2])}]\n"
        f"tags: [{', '.join(primary_keywords + secondary_keywords[:5])}]\n"
        f"featuredImage: {transformed_image_filepath if transformed_image_filepath else 'None'}\n\n"
        f"**Blog Content (following the metadata block):**\n"
        f"1.  **Main Title (H1):** Start with an H1 heading based on the provided `suggested_blog_title`. Example: `# {new_blog_title}`.\n"
        f"2.  **Introduction (2-3 paragraphs):** Hook the reader. Clearly state the problem or topic and your blog's value proposition.\n"
        f"3.  **Main Sections:** Follow the `blog_outline` from `research_output`. Expand each section (`##`) and sub-section (`###`). Ensure each section provides substantial information.\n"
        f"4.  **FAQ Section:** Include 5-7 frequently asked questions with detailed, comprehensive answers, related to the topic and incorporating keywords.\n"
        f"5.  **Conclusion:** Summarize key takeaways, provide a forward-looking statement, and a clear call-to-action.\n"
        f"Do NOT include any introductory or concluding remarks outside the blog content itself (e.g., 'Here is your blog post'). **Do NOT include placeholders (like `example.com`), or any comments intended for me within the output markdown. The entire output must be polished, final content, ready for publication.**"
    )

    try:
        logger.info(f"Generating full blog content for: '{new_blog_title[:70]}...'")
        response = _gemini_generate_content_with_retry(CONTENT_MODEL, prompt)

        content = response.text.strip()

        # Log the raw markdown from the AI
        logger.info(f"--- Raw AI-generated Markdown Content (first 500 chars): ---\n{content[:500]}\n--- End Raw AI Markdown ---")
        logger.info(f"Full raw AI-generated Markdown content length: {len(content)} characters.")

        # Verify metadata block is present
        if not re.search(r"title:\s*.*\n.*?tags:\s*\[.*\]", content, re.DOTALL):
            logger.warning("Generated content appears to be missing the required metadata block!")
            # Add metadata block if missing
            content = (
                f"title: {new_blog_title}\n"
                f"description: {blog_description_for_prompt}\n"
                f"date: {datetime.now().strftime('%Y-%m-%d')}\n"
                f"categories: [{category}, {', '.join(primary_keywords[:2])}]\n"
                f"tags: [{', '.join(primary_keywords + secondary_keywords[:5])}]\n"
                f"featuredImage: {transformed_image_filepath if transformed_image_filepath else 'None'}\n\n"
                f"{content}"
            )
            logger.info("Added missing metadata block to content")

        content = clean_ai_artifacts(content)

        # Log the cleaned markdown
        logger.info(f"--- Cleaned AI-generated Markdown Content (first 500 chars): ---\n{content[:500]}\n--- End Cleaned AI Markdown ---")

        logger.info("Content generation successful.")
        return content

    except Exception as e:
        logger.error(f"Content Agent generation failed for '{new_blog_title}': {e}", exc_info=True)
        return None

def clean_ai_artifacts(content):
    """Remove common low-quality artifacts without destroying valid markdown structure."""
    if not content:
        return ""

    # Remove any stray @ symbols followed by words (e.g., @https, @email), likely from malformed links
    content = re.sub(r'\s*@\S+', '', content)
    # Remove embedded executable or tracking-heavy HTML blocks from LLM output.
    content = re.sub(r'(?is)<script.*?>.*?</script>', '', content)
    content = re.sub(r'(?is)<iframe.*?>.*?</iframe>', '', content)
    content = re.sub(r'(?is)<object.*?>.*?</object>', '', content)

    # Remove example.com or similar placeholder URLs, both in markdown and raw URLs
    placeholder_domains = [
        'example.com', 'example.org', 'placeholder.com', 'yoursite.com',
        'website.com', 'domain.com', 'site.com', 'yourblogname.com', 'ai-generated.com'
    ]
    for domain in placeholder_domains:
        # Markdown links like [text](https://www.example.com/path)
        content = re.sub(rf'\[[^\]]*\]\(https?://(?:www\.)?{re.escape(domain)}[^\)]*\)', '', content, flags=re.IGNORECASE)
        # Raw URLs like https://www.example.com/path
        content = re.sub(rf'https?://(?:www\.)?{re.escape(domain)}\S*', '', content, flags=re.IGNORECASE)

    # Remove common AI wrapper lines while preserving normal prose.
    ai_patterns = [
        r'(?im)^\s*(?:here(?:\'s| is)\s+(?:your|the)\s+blog\s+post|this\s+is\s+the\s+generated\s+content)\s*:?\s*$',
        r'<!--.*?-->', # HTML comments
        r'/\*.*?\*/',   # CSS/JS comments
    ]
    for pattern in ai_patterns:
        content = re.sub(pattern, '', content, flags=re.DOTALL) # DOTALL to match across newlines

    # Clean up multiple consecutive blank lines to just two (for paragraph separation)
    content = re.sub(r'\n\s*\n\s*\n+', '\n\n', content)

    # Remove leading/trailing whitespace from each line
    content = '\n'.join([line.strip() for line in content.split('\n')])

    # Ensure consistent line endings
    content = content.replace('\r\n', '\n').replace('\r', '\n')

    return content.strip()

def parse_markdown_metadata(markdown_content):
    """
    Parses metadata from the top of a markdown string.
    Expected format: key: value\nkey2: value2\n\n# Blog Title...
    Returns a dictionary of metadata and the remaining blog content.
    """
    metadata = {}
    lines = markdown_content.split('\n')
    content_start_index = 0

    logger.debug("Starting metadata parsing...")
    for i, line in enumerate(lines):
        stripped_line = line.strip()
        if not stripped_line: # Found a blank line, metadata block ends here
            content_start_index = i + 1
            logger.debug(f"Blank line found, metadata ends at line {i}. Content starts at {content_start_index}.")
            break
        if ':' in stripped_line: # It's a metadata line
            key, value = stripped_line.split(':', 1)
            metadata[key.strip()] = value.strip()
            logger.debug(f"Parsed metadata line: {key.strip()}: {value.strip()}")
        else: # Not a metadata line and not blank, so metadata ended unexpectedly
            content_start_index = i
            logger.warning(f"Metadata block ended unexpectedly at line {i} with: '{stripped_line}'")
            break
    else:
        content_start_index = len(lines)
        logger.debug("No blank line found, assuming all content is metadata or empty after checking.")

    blog_content_only = '\n'.join(lines[content_start_index:]).strip()

    # The H1 title is usually the first heading in the content body.
    if blog_content_only.startswith('# '):
        h1_line_end = blog_content_only.find('\n')
        if h1_line_end != -1:
            h1_title = blog_content_only[2:h1_line_end].strip() # Skip '# '
            if 'title' not in metadata: # Prioritize already parsed metadata title
                metadata['title'] = h1_title
            blog_content_only = blog_content_only[h1_line_end:].strip()
            logger.debug(f"Extracted H1 title: '{h1_title}'. Remaining content starts after H1.")
        else: # H1 is the only line
            h1_title = blog_content_only[2:].strip()
            if 'title' not in metadata:
                metadata['title'] = h1_title
            blog_content_only = "" # Content is just the H1
            logger.debug(f"Extracted H1 title (only line): '{h1_title}'. Content became empty.")

    logger.info(f"Final parsed metadata: {metadata}")
    logger.info(f"Blog content starts with: {blog_content_only[:100]}...") # Log beginning of content

    return metadata, blog_content_only

def markdown_to_html(markdown_text, main_featured_image_filepath=None, main_featured_image_b64_data_uri=None):
    """
    Converts a subset of Markdown to HTML.
    If main_featured_image_filepath and main_featured_image_b64_data_uri are provided,
    it will replace img src that matches main_featured_image_filepath with the Base64 URI.
    Includes cleanup for malformed example links and AI instructions.
    """
    html_text = markdown_text

    # Apply aggressive cleanup before markdown conversion
    html_text = clean_ai_artifacts(html_text)
    # Escape any raw HTML so only generated template tags remain executable.
    html_text = html.escape(html_text, quote=False)

    # Convert headings (order matters: h3 before h2 before h1 to prevent partial matches)
    html_text = re.sub(r'###\s*(.*)', lambda m: f'<h3>{m.group(1).strip()}</h3>', html_text)
    html_text = re.sub(r'##\s*(.*)', lambda m: f'<h2>{m.group(1).strip()}</h2>', html_text)
    # H1 is assumed to be handled by the template based on metadata, so it's stripped if found in content body.
    html_text = re.sub(r'#\s*(.*)', lambda m: f'<h1>{m.group(1).strip()}</h1>', html_text) # Catch any H1s not stripped by metadata parser

    # Bold and Italic
    html_text = re.sub(r'\*\*\*(.*?)\*\*\*', r'<strong><em>\1</em></strong>', html_text) # Bold Italic
    html_text = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', html_text) # Bold
    html_text = re.sub(r'_(.*?)_', r'<em>\1</em>', html_text) # Italic (underscores)
    html_text = re.sub(r'\*(.*?)\*', r'<em>\1</em>', html_text) # Italic (asterisks)

    # Lists (unordered and ordered)
    html_text = re.sub(
        r'^\s*([-*]|\d+\.)\s+(.*)$',
        lambda m: f'<li data-list-type="{"ol" if m.group(1).endswith(".") else "ul"}">{m.group(2)}</li>',
        html_text,
        flags=re.MULTILINE
    )

    def wrap_lists(match):
        block = match.group(0)
        list_type = "ol" if 'data-list-type="ol"' in block else "ul"
        block = re.sub(r'\sdata-list-type="(?:ul|ol)"', '', block)
        return f'<{list_type}>{block}</{list_type}>'

    html_text = re.sub(r'(<li\s+data-list-type="(?:ul|ol)">.*?</li>\s*)+', wrap_lists, html_text, flags=re.DOTALL)


    # Images - this is where we inject Base64 if it's the specific transformed image
    def image_replacer(match):
        alt_text = match.group(1)
        src_url = match.group(2)

        # Check if the markdown image URL matches our transformed image's file path
        # Use os.path.basename to compare just the filename part, as the agent might give a full path
        if main_featured_image_filepath and os.path.basename(src_url) == os.path.basename(main_featured_image_filepath):
            logger.info(f"Replacing markdown image link '{src_url}' with Base64 data URI for in-content display.")
            safe_alt_text = alt_text.replace('"', '&quot;')
            safe_src = sanitize_external_url(main_featured_image_b64_data_uri, allow_data=True)
            return f'<img src="{safe_src}" alt="{safe_alt_text}" class="in-content-image">'
        else:
            safe_alt_text = alt_text.replace('"', '&quot;')
            safe_src = sanitize_external_url(src_url)
            return f'<img src="{safe_src}" alt="{safe_alt_text}" class="in-content-image">'

    html_text = re.sub(r'!\[(.*?)\]\((.*?)\)', image_replacer, html_text)


    # Links
    def link_replacer(match):
        text = match.group(1)
        href = sanitize_external_url(match.group(2))
        return f'<a href="{href}" target="_blank" rel="noopener noreferrer">{text}</a>'

    html_text = re.sub(r'\[(.*?)\]\((.*?)\)', link_replacer, html_text)

    # Paragraphs (wrap blocks of text not already wrapped by other block-level elements)
    lines = html_text.split('\n')
    parsed_lines = []
    current_paragraph_lines = []

    # Regex to identify common block-level HTML tags
    block_tags_re = re.compile(r'^\s*<(h\d|ul|ol|li|img|a|div|p|blockquote|pre|table|script|style|br)', re.IGNORECASE)

    for line in lines:
        stripped_line = line.strip()
        if not stripped_line: # Empty line signals end of paragraph
            if current_paragraph_lines:
                # Join lines, remove multiple spaces, and ensure content
                para_content = ' '.join(current_paragraph_lines).strip()
                if para_content: # Only add if there's actual content
                    parsed_lines.append(f"<p>{para_content}</p>")
                current_paragraph_lines = []
            parsed_lines.append('') # Keep the blank line for visual separation in output
        elif block_tags_re.match(stripped_line): # This line is already a block-level element
            if current_paragraph_lines:
                para_content = ' '.join(current_paragraph_lines).strip()
                if para_content:
                    parsed_lines.append(f"<p>{para_content}</p>")
                current_paragraph_lines = []
            parsed_lines.append(line) # Add the block-level element directly
        else:
            current_paragraph_lines.append(line)

    # Add any remaining paragraph content at the end
    if current_paragraph_lines:
        para_content = ' '.join(current_paragraph_lines).strip()
        if para_content:
            parsed_lines.append(f"<p>{para_content}</p>")

    final_html_content = '\n'.join(parsed_lines)

    # Final cleanup of empty paragraph tags that might have been created
    final_html_content = re.sub(r'<p>\s*</p>', '', final_html_content)
    final_html_content = re.sub(r'<p><br\s*/?></p>', '', final_html_content)
    final_html_content = re.sub(r'<h1>(.*?)</h1>', r'<h2>\1</h2>', final_html_content) # Ensure no H1 from content

    return final_html_content

def generate_enhanced_html_template(title, description, keywords, image_url_for_seo,
                                  image_src_for_html_body, html_blog_content,
                                  category, article_url_for_disclaimer, published_date):
    """Generate enhanced HTML template with better styling and comprehensive SEO elements."""

    escaped_title_html = html.escape(title or "", quote=True)
    escaped_description_html = html.escape(description or "", quote=True)
    escaped_keywords_html = html.escape(keywords or "", quote=True)
    escaped_title_text = html.escape(title or "", quote=False)
    escaped_category_text = html.escape((category or "general").upper(), quote=False)

    safe_article_url = sanitize_external_url(article_url_for_disclaimer)
    safe_image_url_for_seo = sanitize_external_url(image_url_for_seo, allow_data=True)
    safe_image_src_for_html_body = sanitize_external_url(image_src_for_html_body, allow_data=True)

    # Use json.dumps to get correctly escaped strings for JSON values.
    # We then slice [1:-1] to remove the outer quotes added by json.dumps,
    # as the f-string already provides them for the JSON-LD structure.
    json_safe_title = json.dumps(title or "")[1:-1]
    json_safe_description = json.dumps(description or "")[1:-1]

    # Enhanced structured data (JSON-LD)
    structured_data = f"""
    <script type="application/ld+json">
    {{
      "@context": "https://schema.org",
      "@type": "NewsArticle",
      "headline": "{json_safe_title}",
      "image": ["{safe_image_url_for_seo}"],
      "datePublished": "{published_date}T00:00:00Z",
      "dateModified": "{published_date}T00:00:00Z",
      "articleSection": "{category.capitalize()}",
      "author": {{
        "@type": "Organization",
        "name": "AI Content Creator"
      }},
      "publisher": {{
        "@type": "Organization",
        "name": "Your Publication Name",
        "logo": {{
          "@type": "ImageObject",
          "url": "{safe_image_url_for_seo}"
        }}
      }},
      "mainEntityOfPage": {{
        "@type": "WebPage",
        "@id": "{safe_article_url}"
      }},
      "description": "{json_safe_description}"
    }}
    </script>
    """

    # Styles for the HTML page
    html_styles = """
    <style>
        :root {
            --primary-color: #3498db;
            --secondary-color: #2c3e50;
            --text-color: #333;
            --light-bg: #f5f7fa;
            --card-bg: #ffffff;
            --border-color: #e0e0e0;
            --shadow-light: 0 4px 15px rgba(0,0,0,0.08);
            --shadow-hover: 0 6px 20px rgba(0,0,0,0.12);
        }

        body {
            font-family: 'Segoe UI', 'Helvetica Neue', Arial, sans-serif;
            line-height: 1.7;
            color: var(--text-color);
            background: var(--light-bg);
            margin: 0;
            padding: 0;
            -webkit-font-smoothing: antialiased;
            -moz-osx-font-smoothing: grayscale;
        }

        .container {
            max-width: 850px;
            margin: 30px auto;
            padding: 25px;
            background: var(--card-bg);
            border-radius: 12px;
            box-shadow: var(--shadow-light);
            transition: all 0.3s ease-in-out;
        }
        .container:hover {
            box-shadow: var(--shadow-hover);
        }

        .article-header {
            text-align: center;
            margin-bottom: 30px;
            padding-bottom: 20px;
            border-bottom: 1px solid var(--border-color);
        }

        .category-tag {
            display: inline-block;
            background: var(--primary-color);
            color: white;
            padding: 8px 18px;
            border-radius: 20px;
            font-size: 0.85em;
            font-weight: 600;
            letter-spacing: 0.8px;
            margin-bottom: 15px;
            text-transform: uppercase;
        }

        h1 {
            font-size: 2.2em;
            color: var(--secondary-color);
            margin-bottom: 15px;
            line-height: 1.3;
        }
        h2 {
            font-size: 1.7em;
            color: var(--secondary-color);
            margin-top: 30px;
            margin-bottom: 15px;
            padding-bottom: 5px;
            border-bottom: 1px dashed var(--border-color);
        }
        h3 {
            font-size: 1.3em;
            color: var(--secondary-color);
            margin-top: 25px;
            margin-bottom: 10px;
        }

        p {
            margin-bottom: 1.2em;
        }

        .featured-image {
            width: 100%;
            height: auto;
            /* Max height defined relative to its container to keep aspect ratio of 16:9 for main image part */
            /* This height will be based on the *original content height* of the transformed image (675px) */
            max-height: 843.75px; /* 1200 * (675 + 0.25*675) / 1200 = 1200 * 843.75 / 1200 */
            object-fit: cover;
            border-radius: 8px;
            margin-top: 25px;
            margin-bottom: 30px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }

        .in-content-image {
            max-width: 100%;
            height: auto;
            display: block;
            margin: 2em auto;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }

        a {
            color: var(--primary-color);
            text-decoration: none;
            transition: color 0.2s ease-in-out;
        }
        a:hover {
            color: #1a5e8c;
            text-decoration: underline;
        }

        ul, ol {
            margin-left: 25px;
            margin-bottom: 1.5em;
        }
        li {
            margin-bottom: 0.6em;
        }

        .source-link {
            margin-top: 40px;
            padding-top: 20px;
            border-top: 1px solid var(--border-color);
            font-size: 0.95em;
            text-align: center;
            color: #666;
        }

        @media (max-width: 768px) {
            .container {
                margin: 15px;
                padding: 15px;
            }
            h1 { font-size: 1.8em; }
            h2 { font-size: 1.5em; }
            h3 { font-size: 1.2em; }
            .category-tag { font-size: 0.8em; padding: 6px 14px; }
        }
    </style>
    """
    # The image_src_for_html_body should now represent the full, extended image
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{escaped_title_html}</title>
    <meta name="description" content="{escaped_description_html}">
    <meta name="keywords" content="{escaped_keywords_html}">
    <meta name="robots" content="index, follow">
    <meta name="author" content="AI Content Creator">

    <!-- Open Graph / Facebook -->
    <meta property="og:type" content="article">
    <meta property="og:url" content="{safe_article_url}">
    <meta property="og:title" content="{escaped_title_html}">
    <meta property="og:description" content="{escaped_description_html}">
    <meta property="og:image" content="{safe_image_url_for_seo}">

    <!-- Twitter -->
    <meta property="twitter:card" content="summary_large_image">
    <meta property="twitter:url" content="{safe_article_url}">
    <meta property="twitter:title" content="{escaped_title_html}">
    <meta property="twitter:description" content="{escaped_description_html}">
    <meta property="twitter:image" content="{safe_image_url_for_seo}">

    {structured_data}
    {html_styles}
</head>
<body>
    <div class="container">
        <div class="article-header">
            <span class="category-tag">{escaped_category_text}</span>
            <h1>{escaped_title_text}</h1>
            {f'<img src="{safe_image_src_for_html_body}" alt="{escaped_title_html}" class="featured-image">' if image_src_for_html_body else ''}
        </div>
        <div class="article-content">
            {html_blog_content}
        </div>
        <div class="source-link">
            <p><strong>Disclaimer:</strong> This article was generated with AI assistance and reviewed by automated quality checks before publishing.</p>
            <p>A primary source contributing to this content can be found here: <a href="{safe_article_url}" target="_blank" rel="noopener noreferrer">{safe_article_url}</a></p>
        </div>
    </div>
</body>
</html>"""

def extract_post_metadata_from_html(html_content):
    """Extract title and label candidates from generated HTML."""
    title = "Generated Blog Post"
    labels = []

    title_match = re.search(r'<title>(.*?)</title>', html_content, re.IGNORECASE | re.DOTALL)
    if title_match:
        title = html.unescape(title_match.group(1).strip())

    keywords_match = re.search(
        r'<meta\s+name=["\']keywords["\']\s+content=["\'](.*?)["\']',
        html_content,
        re.IGNORECASE | re.DOTALL
    )
    if keywords_match:
        raw_keywords = html.unescape(keywords_match.group(1))
        for part in raw_keywords.split(','):
            cleaned = part.strip().replace('_', ' ')
            if cleaned:
                labels.append(cleaned)

    category_match = re.search(r'<span class="category-tag">(.*?)</span>', html_content, re.IGNORECASE | re.DOTALL)
    if category_match:
        category = html.unescape(category_match.group(1).strip()).lower()
        if category:
            labels.append(category)

    deduped_labels = []
    seen = set()
    for label in labels:
        normalized = label.lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            deduped_labels.append(normalized)

    return title, deduped_labels

# --- MODIFIED post_to_blogger function (to accept UserCredentials) ---
def post_to_blogger(html_file_path, blog_id, blogger_user_credentials):
    """
    Posts a generated HTML blog to Blogger.
    """
    if not blogger_user_credentials or not blogger_user_credentials.valid:
        logger.error("Blogger User Credentials are not valid. Cannot post to Blogger.")
        return False

    try:
        blogger_service = build('blogger', 'v3', credentials=blogger_user_credentials)

        # Read the HTML content and parse metadata
        with open(html_file_path, 'r', encoding='utf-8') as f:
            full_html_content = f.read()

        logger.info("Extracting metadata from generated HTML...")
        post_title, post_labels = extract_post_metadata_from_html(full_html_content)
        logger.info(f"Resolved post title: {post_title}")
        logger.info(f"Resolved post labels: {post_labels}")

        # If no labels were found, add some default ones based on the title
        if not post_labels:
            logger.warning("No labels found in metadata. Adding default labels based on title...")
            # Extract words from title and use them as labels
            default_labels = [word.lower() for word in re.findall(r'\w+', post_title) if len(word) > 3]
            post_labels.extend(default_labels[:5])  # Add up to 5 words from title
            logger.info(f"Added default labels: {post_labels}")

        post_body = {
            'kind': 'blogger#post',
            'blog': {'id': blog_id},
            'title': post_title,
            'content': full_html_content,
            'labels': post_labels,
            'status': BLOGGER_POST_STATUS
        }
        logger.info(f"Preparing Blogger post with:")
        logger.info(f"Title: {post_title}")
        logger.info(f"Labels: {post_labels}")
        logger.info(f"Status: {BLOGGER_POST_STATUS}")
        logger.info(f"Content length: {len(full_html_content)} characters")

        logger.info(f"Attempting to insert blog post to Blogger: '{post_title}' with labels: {post_labels}...")
        request = blogger_service.posts().insert(blogId=blog_id, body=post_body)
        response = request.execute()

        logger.info(f"✅ Successfully posted '{post_title}' to Blogger! Post ID: {response.get('id')}")
        logger.info(f"View live at: {response.get('url')}")
        # Look for the 'labels' key in the API response
        response_labels = response.get('labels', [])
        logger.info(f"Blogger API Response labels: {response_labels}")
        
        # Verify if labels were actually set
        if not response_labels:
            logger.warning("No labels found in Blogger API response. Labels may not have been set correctly.")
        elif set(response_labels) != set(post_labels):
            logger.warning(f"Labels mismatch! Sent: {post_labels}, Received: {response_labels}")
        
        return True

    except HttpError as e:
        error_content = e.content.decode('utf-8')
        logger.error(f"Failed to post to Blogger due to API error: {e}")
        logger.error(f"Error details: {error_content}")
        if "rateLimitExceeded" in error_content:
            logger.error("Blogger API rate limit exceeded. Consider reducing posting frequency.")
        elif "User lacks permission" in str(e) or "insufficient permission" in str(e).lower():
            logger.error("Blogger: User lacks permission to post. Ensure the authenticated Google account has Author/Admin rights on the target blog.")
        return False
    except Exception as e:
        logger.critical(f"An unexpected error occurred during Blogger posting: {e}", exc_info=True)
        return False


def save_blog_post(consolidated_topic_for_fallback, generated_markdown_content, category, transformed_image_filepath, transformed_image_b64, primary_source_url):
    """
    Saves the generated blog post in an HTML file with SEO elements.
    Accepts the *transformed_image_filepath* for SEO metadata and *transformed_image_b64* for inline HTML.
    `primary_source_url` is used for the disclaimer link.
    Returns the file path of the saved HTML blog.
    """
    # 1. Parse metadata from the top of the markdown content
    metadata, blog_content_only_markdown = parse_markdown_metadata(generated_markdown_content)

    # Use parsed metadata, with fallbacks to consolidated_topic_for_fallback
    title = metadata.get('title', consolidated_topic_for_fallback)

    # Safe fallback for description, ensuring it's not too long and doesn't contain quotes
    description_fallback = f"A comprehensive look at the latest news in {category} related to '{title}'."
    description = metadata.get('description', description_fallback).replace('\n', ' ').replace('\r', ' ').strip()[:155]

    parsed_tags = parse_metadata_list_field(metadata.get('tags', ''))
    if not parsed_tags: # Fallback if agent didn't provide tags
        keywords = ', '.join([category, 'news', 'latest', sanitize_filename(title)[:30]])
    else:
        keywords = ', '.join(parsed_tags).lower()

    # Use the Base64 URI for the main image in the HTML body
    image_src_for_html_body = transformed_image_b64 if transformed_image_b64 else ''

    # Until image upload URL is integrated, use same inline source for social metadata.
    image_url_for_seo = image_src_for_html_body

    published_date = metadata.get('date', datetime.now().strftime('%Y-%m-%d'))

    # 2. Convert markdown content to HTML, applying Base64 for the specific transformed image
    html_blog_content = markdown_to_html(
        blog_content_only_markdown,
        main_featured_image_filepath=transformed_image_filepath,
        main_featured_image_b64_data_uri=transformed_image_b64
    )

    # Clean title for file system for the HTML file itself
    safe_title_for_file = sanitize_filename(title)

    folder = os.path.join(BLOG_OUTPUT_FOLDER, category)
    os.makedirs(folder, exist_ok=True)
    file_path = os.path.join(folder, f"{safe_title_for_file}.html")

    # Generate the complete HTML using the enhanced template
    final_html_output = generate_enhanced_html_template(
        title, description, keywords, image_url_for_seo,
        image_src_for_html_body, html_blog_content,
        category, primary_source_url, published_date
    )

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(final_html_output)
    logger.info(f"✅ Saved blog post: {file_path}")
    return file_path


# --- MODIFIED main function ---
def main():
    blogger_oauth_credentials = None # Initialize credentials variable

    if not validate_environment():
        logger.error("Environment validation failed. Exiting.")
        return

    # --- Authenticate with Blogger using OAuth 2.0 (User Credentials) ---
    # This will try to load from secrets, then local files, then interactive flow
    if BLOGGER_BLOG_ID: # Only try to get creds if BLOG_ID is set
        logger.info("\n--- Authenticating with Blogger using OAuth 2.0 ---")
        blogger_oauth_credentials = get_blogger_oauth_credentials()
        if not blogger_oauth_credentials:
            logger.critical("CRITICAL: Failed to obtain Blogger OAuth credentials. Cannot post to Blogger. Exiting.")
            return
        logger.info("--- Blogger OAuth Authentication Successful ---\n")
    else:
        logger.warning("INFO: BLOGGER_BLOG_ID not configured. Blogger posting will be skipped.")


    global_competitors = [
        "forbes.com", "reuters.com", "bloomberg.com", "theverge.com",
        "techcrunch.com", "healthline.com", "webmd.com", "espn.com",
        "investopedia.com", "zdnet.com", "cnet.com", "medicalnewstoday.com",
        "bbc.com/news", "cnn.com", "nytimes.com"
    ]

    categories_for_run = CATEGORIES[:]
    random.shuffle(categories_for_run)
    categories_for_run = categories_for_run[:min(MAX_CATEGORIES_PER_RUN, len(categories_for_run))]
    logger.info(f"Selected categories for this run: {categories_for_run}")

    for category in categories_for_run:
        logger.info(f"\n--- Starting processing for category: [{category.upper()}] ---")

        raw_articles = fetch_gnews_articles(category, max_articles_to_fetch=NUM_SOURCE_ARTICLES_TO_AGGREGATE)

        if not raw_articles:
            logger.info(f"No raw articles fetched for {category}. Skipping category.")
            continue

        consolidated_data = aggregate_articles(raw_articles, category)

        if not consolidated_data:
            logger.error(f"Failed to aggregate articles for {category}. Skipping blog generation.")
            continue

        consolidated_topic = consolidated_data['consolidated_topic']
        consolidated_image_url = consolidated_data['image_url']
        consolidated_description = consolidated_data['combined_description']
        consolidated_content_for_ai = consolidated_data['combined_content']
        primary_source_url_for_disclaimer = consolidated_data['primary_source_url']

        effective_competitors = list(set(global_competitors + consolidated_data['competitors']))

        logger.info(f"\n  Starting workflow for consolidated topic: '{consolidated_topic[:70]}...'")

        transformed_image_filepath = None
        transformed_image_b64 = None
        if consolidated_image_url:
            safe_image_filename = sanitize_filename(consolidated_topic)
            transformed_image_filepath, transformed_image_b64 = transform_image(
                consolidated_image_url,
                consolidated_topic, # Use the consolidated topic for image text
                category,
                category, # Output subfolder for images
                safe_image_filename # Filename base (extension will be added by transform_image)
            )
        else:
            logger.info("  No consolidated image URL provided. Skipping image transformation for this blog.")

        try:
            # Prepare data for content agent to synthesize from
            consolidated_article_data_for_ai = {
                "consolidated_topic": consolidated_topic,
                "combined_description": consolidated_description,
                "combined_content": consolidated_content_for_ai,
                "category": category,
                "original_image_url_selected": consolidated_image_url # Inform AI which image was selected
            }

            # --- Step 1: Research Agent (Gemini Call 1) ---
            if GEMINI_API_KEY:
                research_output = perform_research_agent(consolidated_topic, effective_competitors)
                if not research_output:
                    logger.error(f"Failed to get research output for: '{consolidated_topic}'. Skipping content generation.")
                    continue
                logger.info(f"  Research successful. Suggested Title: '{research_output.get('suggested_blog_title', 'N/A')}'")
                logger.info(f"  Primary Keywords: {research_output.get('primary_keywords', [])}")

                # --- Step 2: Content Generator Agent (Gemini Call 2) ---
                generated_blog_markdown = generate_content_agent(
                    consolidated_article_data_for_ai,
                    research_output,
                    transformed_image_filepath
                )

                if not generated_blog_markdown:
                    logger.error(f"Failed to generate blog content for: '{consolidated_topic}'. Skipping save.")
                    continue
            else:
                logger.warning("GEMINI_API_KEY is not set. Skipping AI content generation. Only image processing and basic HTML saving will occur (if possible).")
                placeholder_description = (
                    consolidated_description
                    .replace('\n', ' ')
                    .replace('\r', ' ')
                    .strip()[:155]
                    .replace("'", "")
                )
                generated_blog_markdown = (
                    f"title: {consolidated_topic}\n"
                    f"description: {placeholder_description}\n"
                    f"date: {datetime.now().strftime('%Y-%m-%d')}\n"
                    f"categories: [{category}]\n"
                    f"tags: [{category}, news]\n"
                    f"featuredImage: {transformed_image_filepath or 'None'}\n\n"
                    f"# {consolidated_topic}\n\n"
                    f"<p>This is a placeholder blog post because AI generation was skipped due to missing API key.</p>\n"
                    f"<p>Original aggregated content details (first 500 chars): {consolidated_content_for_ai[:500]}...</p>"
                )
                research_output = {"primary_keywords": [], "secondary_keywords": {}, "competitor_insights": "", "blog_outline": "", "suggested_blog_title": consolidated_topic}


            # --- Step 3: Save the blog post ---
            saved_html_file_path = save_blog_post(
                consolidated_topic,
                generated_blog_markdown,
                category,
                transformed_image_filepath,
                transformed_image_b64,
                primary_source_url_for_disclaimer
            )

            # --- Step 4: Post to Blogger ---
            if saved_html_file_path and blogger_oauth_credentials and BLOGGER_BLOG_ID:
                post_to_blogger(
                    saved_html_file_path,
                    BLOGGER_BLOG_ID,
                    blogger_oauth_credentials
                )
            else:
                logger.warning("Skipping Blogger post due to missing HTML file or Blogger credentials/ID.")

        except Exception as e:
            logger.critical(f"An unexpected error occurred during blog generation workflow for '{consolidated_topic}': {e}", exc_info=True)
        finally:
            time.sleep(30) # Delay between categories to avoid hitting API rate limits too quickly

if __name__ == '__main__':
    main()
