from flask import Flask, render_template, request, jsonify, redirect, Response, send_file
import requests
import pandas as pd
import json
import os
from urllib.parse import quote_plus # <--- ADD THIS IMPORT
from datetime import datetime
import datetime
import time
import random
import hashlib
import base64
from premailer import transform
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import uuid
import csv
import io
import re
import psycopg2
from psycopg2 import sql
from urllib.parse import urlparse
import glob
import logging # Import logging for more structured output
import traceback # Import traceback for printing full stack traces

# Configure basic logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Add dotenv support for local development
try:
    from dotenv import load_dotenv
    load_dotenv()
    logging.info("SUCCESS: .env file loaded for local development.")
except ImportError:
    logging.info('INFO: python-dotenv not installed; .env file will not be loaded. This is normal for production.')

app = Flask(__name__)

# --- Configuration for Render Deployment ---
PORT = int(os.environ.get("PORT", 5000))
BASE_URL = os.environ.get("BASE_URL", f"http://localhost:{PORT}")
logging.info(f"INFO: Application will use BASE_URL: {BASE_URL}")

# --- Supabase Database Configuration (Explicit & Robust) ---
DB_HOST = os.environ.get("DB_HOST")
DB_NAME = os.environ.get("DB_NAME")
DB_USER = os.environ.get("DB_USER")
DB_PASSWORD = os.environ.get("DB_PASSWORD")
DB_PORT = os.environ.get("DB_PORT")
GROQ_API_KEY = os.getenv('GROQ_API_KEY')

SSL_CERT_PATH = "supabase_ca.crt"


# --- Environment Variable File Creation ---
if os.environ.get('GOOGLE_CREDENTIALS_JSON_B64'):
    try:
        decoded_credentials = base64.b64decode(os.environ['GOOGLE_CREDENTIALS_JSON_B64']).decode('utf-8')
        with open('credentials.json', 'w') as f:
            f.write(decoded_credentials)
        logging.info("SUCCESS: credentials.json created from environment variable.")
    except Exception as e:
        logging.error(f"ERROR: Could not decode or write GOOGLE_CREDENTIALS_JSON_B64: {e}")

if os.environ.get('GOOGLE_TOKEN_JSON_B64'):
    try:
        decoded_token = base64.b64decode(os.environ['GOOGLE_TOKEN_JSON_B64']).decode('utf-8')
        with open('token.json', 'w') as f:
            f.write(decoded_token)
        logging.info("SUCCESS: token.json created from environment variable.")
    except Exception as e:
        logging.error(f"ERROR: Could not decode or write GOOGLE_TOKEN_JSON_B64: {e}")

if os.environ.get('SUPABASE_SSL_CERT'):
    try:
        with open(SSL_CERT_PATH, 'w') as f:
            f.write(os.environ['SUPABASE_SSL_CERT'])
        logging.info(f"SUCCESS: {SSL_CERT_PATH} created from environment variable.")
    except Exception as e:
        logging.error(f"ERROR: Could not create {SSL_CERT_PATH} from environment variable: {e}")


def get_db_connection():
    """
    Establishes a robust connection to the Supabase PostgreSQL database
    using individual connection parameters and a required SSL certificate.
    """
    if not all([DB_HOST, DB_NAME, DB_USER, DB_PASSWORD, DB_PORT]):
        logging.error("One or more database environment variables (DB_HOST, DB_NAME, DB_USER, DB_PASSWORD, DB_PORT) are not set.")
        raise ValueError("One or more database environment variables (DB_HOST, DB_NAME, DB_USER, DB_PASSWORD, DB_PORT) are not set.")

    if not os.path.exists(SSL_CERT_PATH):
        logging.error(f"SSL certificate not found at {SSL_CERT_PATH}. Ensure the SUPABASE_SSL_CERT environment variable is set and correct.")
        raise FileNotFoundError(f"SSL certificate not found at {SSL_CERT_PATH}. Ensure the SUPABASE_SSL_CERT environment variable is set and correct.")

    try:
        conn = psycopg2.connect(
            host=DB_HOST,
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
            port=DB_PORT,
            sslmode='require',
            sslrootcert=SSL_CERT_PATH
        )
        return conn
    except psycopg2.OperationalError as e:
        logging.critical(f"ERROR: Could not connect to the Supabase database. Please check your connection details and firewall settings. Details: {e}")
        raise

# --- Database Initialization ---
def init_db():
    """Initialize PostgreSQL database tables."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # Campaigns table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS campaigns (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, company_name TEXT NOT NULL,
                product_name TEXT NOT NULL, offer_details TEXT NOT NULL, campaign_type TEXT NOT NULL,
                target_audience TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'draft', total_recipients INTEGER DEFAULT 0
            )
        ''')
        # Email variations table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS email_variations (
                id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, variation_name TEXT NOT NULL,
                subject_line TEXT NOT NULL, email_body TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (campaign_id) REFERENCES campaigns (id) ON DELETE CASCADE
            )
        ''')
        # Recipients table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS recipients (
                id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, email_address TEXT NOT NULL,
                first_name TEXT, last_name TEXT, variation_assigned TEXT NOT NULL,
                sent_at TIMESTAMP, opened_at TIMESTAMP, clicked_at TIMESTAMP, converted_at TIMESTAMP,
                status TEXT DEFAULT 'pending', tracking_id TEXT UNIQUE,
                FOREIGN KEY (campaign_id) REFERENCES campaigns (id) ON DELETE CASCADE
            )
        ''')
        # A/B test results table (PostgreSQL uses SERIAL for auto-increment)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ab_results (
                id SERIAL PRIMARY KEY, campaign_id TEXT NOT NULL, variation_name TEXT NOT NULL,
                metric_name TEXT NOT NULL, metric_value REAL NOT NULL,
                recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (campaign_id) REFERENCES campaigns (id) ON DELETE CASCADE
            )
        ''')
        conn.commit()
        cursor.close()
        print("SUCCESS: PostgreSQL database tables checked/created successfully!")
    except Exception as e:
        print(f"FATAL ERROR: Could not initialize the database. The application cannot start.")
        print(f"Details: {e}")
        raise
    finally:
        if conn:
            conn.close()
            
# Initialize the database when the app starts.
try:
    init_db()
except Exception:
    exit(1)


# --- API & Model Configurations ---
SCOPES = ['https://www.googleapis.com/auth/gmail.send', 'https://www.googleapis.com/auth/gmail.readonly']
LLAMA_MODEL = "meta-llama/Meta-Llama-3-8B-Instruct"
HF_API_URL = f"https://api-inference.huggingface.co/models/{LLAMA_MODEL}"
HF_TOKEN = os.getenv('HUGGINGFACE_API_TOKEN')

# Gmail API functions
def authenticate_gmail():
    """Authenticate and return Gmail service object"""
    creds = None
    logging.info("Attempting Gmail authentication...")
    try:
        if os.path.exists('token.json'):
            creds = Credentials.from_authorized_user_file('token.json', SCOPES)
            logging.info("Loaded credentials from token.json.")

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                logging.info("Refreshing expired Gmail credentials...")
                creds.refresh(Request())
                logging.info("Gmail credentials refreshed successfully.")
            else:
                if os.path.exists('credentials.json'):
                    logging.info("No valid Gmail token, starting new OAuth flow...")
                    flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
                    creds = flow.run_local_server(port=0)
                    logging.info("New Gmail credentials obtained via OAuth flow.")
                else:
                    logging.error("credentials.json file not found. Cannot perform initial OAuth. Set GOOGLE_CREDENTIALS_JSON_B64 or ensure file exists.")
                    raise Exception("credentials.json file not found. Download it from Google Cloud Console or set GOOGLE_CREDENTIALS_JSON_B64.")

        with open('token.json', 'w') as token:
            token.write(creds.to_json())
        logging.info("Gmail token.json updated/saved.")
        return build('gmail', 'v1', credentials=creds)
    except Exception as e:
        logging.error(f"CRITICAL: Gmail authentication failed: {e}")
        traceback.print_exc() # Print full traceback for auth errors
        raise # Re-raise to be caught by the route's try-except block

def create_email_message(to_email, subject, body, tracking_id):
    """Create email message with tracking pixel and unsubscribe link"""
    message = MIMEMultipart('alternative')
    message['to'] = to_email
    message['subject'] = subject

    tracking_pixel = f'<img src="{BASE_URL}/pixel/{tracking_id}" width="1" height="1" style="display:none;">'
    
    # Add a simple unsubscribe link. You might want a dedicated unsubscribe route.
    # A more robust solution would be a unique unsubscribe URL for each recipient.
    unsubscribe_link_html = f'<p style="font-size:10px; color:#999999; text-align:center;">If you no longer wish to receive these emails, <a href="{BASE_URL}/unsubscribe/{tracking_id}" style="color:#999999;">unsubscribe here</a>.</p>'
    unsubscribe_link_plain = f"\n\n---\nIf you no longer wish to receive these emails, please reply to this email or visit {BASE_URL}/unsubscribe/{tracking_id} to unsubscribe."

    html_body_with_tracking = body.replace('\n', '<br>') + tracking_pixel + unsubscribe_link_html
    html_body_with_tracking = add_click_tracking(html_body_with_tracking, tracking_id)

    text_part = MIMEText(body + unsubscribe_link_plain, 'plain')
    html_part = MIMEText(html_body_with_tracking, 'html') # <--- THIS LINE WAS MISSING/INCORRECT

    message.attach(text_part)
    message.attach(html_part)

    return {'raw': base64.urlsafe_b64encode(message.as_bytes()).decode()}

def add_click_tracking(html_body, tracking_id):
    """Add click tracking to links in email body"""
    def replace_link(match):
        original_url = match.group(1)
        # Ensure the original_url is URL-encoded if it contains query parameters etc.
        # urllib.parse.quote_plus can be used here for robust encoding.
        tracking_url = f"{BASE_URL}/click/{tracking_id}?url={quote_plus(original_url)}"
        return f'href="{tracking_url}"'

    # Use a non-greedy regex to prevent matching across multiple href attributes
    html_body = re.sub(r'href="([^"]*?)"', replace_link, html_body) # <--- UPDATED REGEX
    return html_body

def send_email_via_gmail(service, email_message):
    """Send email using Gmail API"""
    try:
        message = service.users().messages().send(userId="me", body=email_message).execute()
        logging.info(f"Email sent successfully. Message ID: {message['id']}")
        return {'success': True, 'message_id': message['id']}
    except HttpError as error:
        logging.error(f"Gmail API HttpError: {error.resp.status} - {error.content.decode()}")
        return {'success': False, 'error': str(error)}
    except Exception as e:
        logging.error(f"Unexpected error when sending email via Gmail: {e}")
        traceback.print_exc()
        return {'success': False, 'error': str(e)}

def calculate_ab_metrics(campaign_id):
    """Calculate A/B testing metrics for a campaign"""
    conn = get_db_connection()
    cursor = conn.cursor()

    # Get all variations for this campaign
    cursor.execute(sql.SQL('''
        SELECT DISTINCT variation_assigned FROM recipients
        WHERE campaign_id = %s
    '''), [campaign_id])
    variations = [row[0] for row in cursor.fetchall()]

    metrics = {}

    for variation in variations:
        # Calculate metrics for each variation
        cursor.execute(sql.SQL('''
            SELECT
                COUNT(*) as total_sent,
                COUNT(CASE WHEN opened_at IS NOT NULL THEN 1 END) as opened,
                COUNT(CASE WHEN clicked_at IS NOT NULL THEN 1 END) as clicked,
                COUNT(CASE WHEN converted_at IS NOT NULL THEN 1 END) as converted
            FROM recipients
            WHERE campaign_id = %s AND variation_assigned = %s AND status = 'sent'
        '''), [campaign_id, variation])

        result = cursor.fetchone()
        total_sent, opened, clicked, converted = result

        metrics[variation] = {
            'total_sent': total_sent,
            'opened': opened,
            'clicked': clicked,
            'converted': converted,
            'open_rate': (opened / total_sent * 100) if total_sent > 0 else 0,
            'click_rate': (clicked / total_sent * 100) if total_sent > 0 else 0,
            'conversion_rate': (converted / total_sent * 100) if total_sent > 0 else 0,
            'click_through_rate': (clicked / opened * 100) if opened > 0 else 0
        }

    cursor.close()
    conn.close()
    return metrics
    
def query_huggingface(payload):
    """Query the Hugging Face API using Llama 3 8B"""
    headers = {"Authorization": f"Bearer {HF_TOKEN}"}

    try:
        response = requests.post(HF_API_URL, headers=headers, json=payload, timeout=60)

        if response.status_code == 200:
            return response.json()
        elif response.status_code == 503:
            logging.warning("Hugging Face API: Model is loading. Please wait and try again.")
            return {"error": "Model is loading. Please wait and try again."}
        elif response.status_code == 404:
            logging.error("Hugging Face API: Model not accessible. Check API token permissions.")
            return {"error": "Model not accessible. Check API token permissions."}
        elif response.status_code == 429:
            logging.warning("Hugging Face API: Rate limit exceeded. Please wait before trying again.")
            return {"error": "Rate limit exceeded. Please wait before trying again."}
        else:
            logging.error(f"Hugging Face API request failed with status {response.status_code}: {response.text}")
            return {"error": f"API request failed with status {response.status_code}"}

    except requests.exceptions.Timeout:
        logging.error("Hugging Face API: Request timed out.")
        return {"error": "Request timed out. Please try again."}
    except requests.exceptions.RequestException as e:
        logging.error(f"Hugging Face API: Request failed: {str(e)}")
        return {"error": f"Request failed: {str(e)}"}

def parse_email_variations(generated_text):
    """Parse generated text into variation objects"""
    variations = []

    parts = generated_text.split('VARIATION')

    for i, part in enumerate(parts[1:], 1):
        if i > 2:
            break

        lines = part.strip().split('\n')
        subject = ""
        body_lines = []
        body_started = False

        for line in lines:
            line = line.strip()
            if line.upper().startswith('SUBJECT:'):
                subject = line[8:].strip()
            elif line.upper().startswith('BODY:'):
                body_started = True
            elif body_started:
                body_lines.append(line)

        body = '\n'.join(body_lines).strip()

        if subject and body:
            variations.append({
                'subject': subject,
                'body': body
            })

    if len(variations) < 2:
        logging.warning("Less than two variations parsed from AI response. Using fallback variations.")
        # Re-calling create_fallback_variations to ensure consistency
        # Ensure 'Your Company', 'Your Product', 'a great offer', 'promotional' are consistent with your use case
        fallback_result = create_fallback_variations("Your Company", "Your Product", "a great offer", "promotional")
        # Extract variations from the fallback_result format
        parsed_fallback = parse_email_variations(fallback_result[0]['generated_text']) # <--- Changed this line slightly
        variations = parsed_fallback if len(parsed_fallback) >= 2 else [
            {'subject': 'Exclusive Offer Inside 🎯', 'body': 'We have something special for you...\n\n[Learn More]'},
            {'subject': 'You\'re Going to Love This', 'body': 'This is exactly what you\'ve been waiting for...\n\n[Discover More]'}
        ]

    return variations

def generate_email_variations(company_name, product_name, offer_details, campaign_type, target_audience=""):
    """Generate email variations using AI with enhanced deliverability instructions"""
    prompt = f"""<|begin_of_text|><|start_header_id|>system<|end_header_id|>

You are an expert email marketing copywriter specialized in high deliverability and engagement. Create two completely different marketing email variations for A/B testing. Each should have a unique, professional, and trustworthy approach and tone, designed to pass spam filters while maintaining high conversion potential.

<|eot_id|><|start_header_id|>user<|end_header_id|>

Create TWO different marketing email variations for A/B testing:

Company: {company_name}
Product/Service: {product_name}
Campaign Focus: {offer_details}
Type: {campaign_type}
Audience: {target_audience if target_audience else "General customers"}

Requirements for each email:
- **Subject line:** Concise (under 50 characters), compelling, and avoid all caps or excessive punctuation. Hint at value, create curiosity, or state purpose clearly.
- **Email body:**
    - Professional, clear, concise, and persuasive.
    - Focus on benefits for the recipient.
    - Use natural language; avoid overly salesy or aggressive terms.
    - **Crucially, avoid spam trigger words and phrases (e.g., "free money", "guaranteed income", excessive urgency unless genuinely applicable and communicated professionally).** # <--- NEW/UPDATED INSTRUCTION
    - Employ different psychological triggers for each variation (e.g., scarcity, social proof, fear of missing out, value proposition, problem/solution).
    - Clear and prominent call-to-action (CTA) with descriptive, trust-inspiring text (e.g., "Learn More about [Product Name]", "Get Your Free Guide").
    - Optimized for mobile reading (short paragraphs, good line breaks).
    - **Include a clear and prominent unsubscribe instruction/link placeholder.** # <--- NEW INSTRUCTION
    - **Signature:** Professional closing with company name.
    - Minimal emoji use (0-2 maximum per email, only if it enhances the message).
    - Maintain a good text-to-image ratio (primarily text). # <--- NEW INSTRUCTION
- **Tone:** One variation could be direct and benefit-driven, the other more narrative or curiosity-driven.

Format response strictly as:

VARIATION A:
SUBJECT: [subject line]
BODY: [email content]

VARIATION B:
SUBJECT: [subject line]
BODY: [email content]

<|eot_id|><|start_header_id|>assistant<|end_header_id|>"""

    payload = {
        "inputs": prompt,
        "parameters": {
            "max_new_tokens": 800,
            "temperature": 0.7,
            "top_p": 0.9,
            "do_sample": True,
            "return_full_text": False,
            "stop": ["<|eot_id|>"]
        }
    }

    result = query_huggingface(payload)

    if 'error' in result:
        logging.warning(f"Hugging Face API Error: {result['error']}. Generating fallback variations.")
        return create_fallback_variations(company_name, product_name, offer_details, campaign_type)

    return result

    if 'error' in result:
        logging.warning(f"Hugging Face API Error: {result['error']}. Generating fallback variations.")
        return create_fallback_variations(company_name, product_name, offer_details, campaign_type)

    return result

def create_fallback_variations(company_name, product_name, offer_details, campaign_type):
    """Create fallback variations optimized for A/B testing, with better spam-filter avoidance."""
    logging.info("Creating fallback email variations with improved content.")
    variation_a = {
        'subject': f'{product_name} from {company_name}: Special Invitation', # More professional # <--- UPDATED SUBJECT
        'body': f'''Dear Valued Customer,

We are excited to introduce you to {product_name}, designed to help you with {offer_details}.

Key benefits include:
- Enhanced productivity 
- Streamlined processes
- Reliable support

Discover how {product_name} can make a difference for you today.

[Learn More About {product_name}]

Sincerely,
The {company_name} Team

P.S. Explore the full features and benefits on our website.
'''
    }

    variation_b = {
        'subject': f'Unlock Your Potential with {product_name}', # Benefit-oriented # <--- UPDATED SUBJECT
        'body': f'''Hello,

At {company_name}, we're always looking for ways to provide greater value. That's why we're delighted to share {product_name}, our latest innovation.

This {campaign_type.lower()} is tailored to assist you with {offer_details}. Many customers are already experiencing positive results:
"Absolutely transformed my workflow!" - A Happy User 
"Simple, effective, and powerful." - Another Customer

Ready to see how {product_name} can work for you?

[Get Started with {product_name}]

Warm regards,
The {company_name} Team

P.S. We invite you to visit our site for a detailed overview.
'''
    }

    return [{"generated_text": f"VARIATION A:\nSUBJECT: {variation_a['subject']}\nBODY: {variation_a['body']}\n\nVARIATION B:\nSUBJECT: {variation_b['subject']}\nBODY: {variation_b['body']}"}]

# API Routes
@app.route('/')
def index():
    """Redirects to the A/B Dashboard"""
    return redirect('/ab-dashboard')

@app.route('/ab-dashboard')
def ab_dashboard():
    """A/B testing dashboard"""
    return render_template('ab_dashboard.html', base_url=BASE_URL)

@app.route('/create-campaign', methods=['POST'])
def create_campaign():
    """Create a new A/B testing campaign"""
    try:
        data = request.get_json()
        logging.info(f"Received data for create_campaign: {data}")

        required_fields = ['company_name', 'product_name', 'offer_details', 'campaign_type']
        if not all(field in data and data[field].strip() for field in required_fields):
            logging.warning("Missing required fields for create_campaign.")
            return jsonify({'success': False, 'error': 'Missing required fields'})

        result = generate_email_variations(
            data['company_name'], data['product_name'],
            data['offer_details'], data['campaign_type'],
            data.get('target_audience', '')
        )

        if 'error' in result:
            logging.error(f"Error generating email variations: {result['error']}")
            return jsonify({'success': False, 'error': result['error']})

        variations = parse_email_variations(result[0]['generated_text'])
        logging.info(f"Parsed {len(variations)} email variations.")

        conn = get_db_connection()
        cursor = conn.cursor()

        campaign_id = str(uuid.uuid4())
        cursor.execute(sql.SQL('''
            INSERT INTO campaigns (id, name, company_name, product_name, offer_details, campaign_type, target_audience)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        '''), (
            campaign_id,
            f"{data['company_name']} - {data['campaign_type'].title()}",
            data['company_name'],
            data['product_name'],
            data['offer_details'],
            data['campaign_type'],
            data.get('target_audience', '')
        ))
        logging.info(f"Campaign {campaign_id} created in DB.")

        for i, variation in enumerate(variations):
            variation_id = str(uuid.uuid4())
            cursor.execute(sql.SQL('''
                INSERT INTO email_variations (id, campaign_id, variation_name, subject_line, email_body)
                VALUES (%s, %s, %s, %s, %s)
            '''), (
                variation_id, campaign_id, f"Variation_{chr(65+i)}",
                variation['subject'], variation['body']
            ))
            logging.info(f"Variation {f'Variation_{chr(65+i)}'} saved for campaign {campaign_id}.")

        conn.commit()
        cursor.close()
        conn.close()
        logging.info(f"Campaign {campaign_id} and variations committed to DB.")

        return jsonify({
            'success': True,
            'campaign_id': campaign_id,
            'variations': variations
        })

    except Exception as e:
        logging.error(f"Error in create_campaign: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)})

@app.route('/upload-recipients', methods=['POST'])
def upload_recipients():
    """Upload recipient list and assign variations using a round-robin method for even distribution."""
    try:
        campaign_id = request.form.get('campaign_id')
        if not campaign_id:
            return jsonify({'success': False, 'error': 'Campaign ID required'})

        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No file uploaded'})

        file = request.files['file']
        if file.filename == '':
            return jsonify({'success': False, 'error': 'No file selected'})

        # Read CSV file
        stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
        csv_input = list(csv.DictReader(stream)) # Read all recipients into a list first

        # Get campaign variations
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(sql.SQL('SELECT variation_name FROM email_variations WHERE campaign_id = %s'), [campaign_id])
        variations = [{'variation_name': row[0]} for row in cursor.fetchall()]

        if not variations:
            return jsonify({'success': False, 'error': 'No variations found for this campaign. Cannot assign recipients.'})

        recipients_added = 0
        
        # --- MODIFIED LOGIC: Round-Robin Assignment ---
        for i, row in enumerate(csv_input):
            email = row.get('email', '').strip()
            if not email:
                continue

            # Assign variation by cycling through the variations list
            variation_index = i % len(variations)
            assigned_variation = variations[variation_index]['variation_name']
            
            tracking_id = str(uuid.uuid4())

            cursor.execute(sql.SQL('''
                INSERT INTO recipients (id, campaign_id, email_address, first_name, last_name, variation_assigned, tracking_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            '''), (
                str(uuid.uuid4()), campaign_id, email,
                row.get('first_name', ''), row.get('last_name', ''),
                assigned_variation, tracking_id
            ))
            recipients_added += 1

        # Update campaign total recipients
        cursor.execute(sql.SQL('UPDATE campaigns SET total_recipients = %s WHERE id = %s'), (recipients_added, campaign_id))

        conn.commit()
        cursor.close()
        conn.close()

        return jsonify({
            'success': True,
            'recipients_added': recipients_added,
            'message': f'Successfully uploaded and assigned {recipients_added} recipients evenly.'
        })

    except Exception as e:
        print(f"Error in upload_recipients: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/send-campaign', methods=['POST'])
def send_campaign():
    """Send A/B testing campaign"""
    try:
        data = request.get_json()
        campaign_id = data.get('campaign_id')
        if not campaign_id:
            return jsonify({'success': False, 'error': 'Campaign ID required'})

        # Authenticate Gmail
        try:
            gmail_service = authenticate_gmail()
        except Exception as e:
            return jsonify({'success': False, 'error': f'Gmail authentication failed: {str(e)}'})

        # Get campaign and variations
        conn = get_db_connection()
        cursor = conn.cursor()

        # Get email variations
        cursor.execute(sql.SQL('''
            SELECT variation_name, subject_line, email_body FROM email_variations WHERE campaign_id = %s
        '''), [campaign_id])
        variations = {row[0]: {'subject': row[1], 'body': row[2]} for row in cursor.fetchall()}

        # Get recipients
        cursor.execute(sql.SQL('''
            SELECT id, email_address, first_name, variation_assigned, tracking_id FROM recipients WHERE campaign_id = %s AND status = 'pending'
        '''), [campaign_id])
        recipients = cursor.fetchall()

        sent_count = 0
        errors = []
        print(f"--- Starting to send campaign {campaign_id} to {len(recipients)} recipients ---")

        for recipient_id, email, first_name, variation, tracking_id in recipients:
            print(f"\nProcessing recipient: {email} for variation: {variation}")
            try:
                # Get variation content
                variation_content = variations[variation]

                # Personalize content
                subject = variation_content['subject']
                body = variation_content['body']
                if first_name:
                    body = body.replace('Hi there', f'Hi {first_name}')
                    body = body.replace('Hello!', f'Hello {first_name}!')

                # Create and send email
                print(f" > Creating email message for {email}...")
                email_message = create_email_message(email, subject, body, tracking_id)

                print(f" > Attempting to send via Gmail API...")
                result = send_email_via_gmail(gmail_service, email_message)

                if result['success']:
                    print(f" > SUCCESS: Email sent. Updating status to 'sent' and clearing tracking timestamps.")
                    # Removed time.sleep(5) - it's generally not recommended in web server loops
                    # Update recipient status and explicitly set tracking timestamps to NULL for new tracking
                    cursor.execute(sql.SQL('''
                        UPDATE recipients
                        SET status = 'sent', sent_at = CURRENT_TIMESTAMP, opened_at = NULL, clicked_at = NULL, converted_at = NULL
                        WHERE id = %s
                    '''), [recipient_id])
                    sent_count += 1
                else:
                    print(f" > FAILED: Gmail API returned an error: {result['error']}")
                    errors.append(f'{email}: {result["error"]}')
                    cursor.execute(sql.SQL('''
                        UPDATE recipients SET status = 'failed' WHERE id = %s
                    '''), [recipient_id])
            except Exception as e:
                print(f" > FAILED: An exception occurred: {str(e)}")
                errors.append(f'{email}: {str(e)}')

        # Commit all the database changes at the end of the loop
        conn.commit()
        print(f"--- Campaign sending finished. Committing changes to database. ---")

        # Update campaign status and total recipients (if not already handled by upload)
        cursor.execute(sql.SQL("UPDATE campaigns SET status = %s WHERE id = %s"), ('sent', campaign_id))
        conn.commit()

        cursor.close()
        conn.close()

        if errors:
            return jsonify({
                'success': False,
                'message': f'Campaign sent with {sent_count} successes and {len(errors)} errors.',
                'errors': errors
            })
        else:
            return jsonify({
                'success': True,
                'message': f'Campaign sent successfully to {sent_count} recipients.'
            })

    except Exception as e:
        print(f"Error in send_campaign: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/campaign-results/<campaign_id>')
def campaign_results(campaign_id):
    """Get A/B testing results for a campaign"""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(sql.SQL('SELECT name, status, total_recipients FROM campaigns WHERE id = %s'), [campaign_id])
        campaign = cursor.fetchone()

        cursor.close()

        if not campaign:
            logging.warning(f"Campaign {campaign_id} not found for results.")
            return jsonify({'success': False, 'error': 'Campaign not found'})

        metrics = calculate_ab_metrics(campaign_id)

        return jsonify({
            'success': True,
            'campaign': {
                'name': campaign[0],
                'status': campaign[1],
                'total_recipients': campaign[2]
            },
            'metrics': metrics
        })

    except Exception as e:
        logging.error(f"Error in campaign_results for {campaign_id}: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)})
    finally:
        if conn:
            conn.close()

@app.route('/campaigns')
def list_campaigns():
    """List all campaigns"""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(sql.SQL('SELECT id, name, status, total_recipients, created_at FROM campaigns ORDER BY created_at DESC'))
        campaigns = [
            {
                'id': row[0],
                'name': row[1],
                'status': row[2],
                'total_recipients': row[3],
                'created_at': row[4]
            }
            for row in cursor.fetchall()
        ]

        cursor.close()

        return jsonify({'success': True, 'campaigns': campaigns})

    except Exception as e:
        logging.error(f"Error in list_campaigns: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)})
    finally:
        if conn:
            conn.close()


# Tracking routes
@app.route('/pixel/<tracking_id>')
def tracking_pixel(tracking_id):
    """Track email opens"""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(sql.SQL('''
            UPDATE recipients
            SET opened_at = CURRENT_TIMESTAMP
            WHERE tracking_id = %s AND opened_at IS NULL
        '''), [tracking_id])

        conn.commit()
        cursor.close()
        logging.info(f"Tracking pixel hit for {tracking_id}. Open recorded.")

        pixel = base64.b64decode('R0lGODlhAQABAIAAAAAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7')
        return Response(pixel, mimetype='image/gif')

    except Exception as e:
        logging.error(f"Error tracking pixel for {tracking_id}: {e}")
        traceback.print_exc()
        pixel = base64.b64decode('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7')
        return Response(pixel, mimetype='image/gif')
    finally:
        if conn:
            conn.close()

@app.route('/click/<tracking_id>')
def track_click(tracking_id):
    """Track email clicks and redirect"""
    conn = None # Initialize conn to None
    try:
        original_url = request.args.get('url', BASE_URL) # Fallback to BASE_URL

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(sql.SQL('''
            UPDATE recipients
            SET clicked_at = CURRENT_TIMESTAMP
            WHERE tracking_id = %s AND clicked_at IS NULL
        '''), [tracking_id])

        conn.commit()
        cursor.close()
        # conn.close() # Close conn in finally block

        return redirect(original_url)

    except Exception as e:
        print(f"Error tracking click for {tracking_id}: {e}")
        return redirect(BASE_URL) # Redirect to BASE_URL on error
    finally:
        if conn:
            conn.close()

def parse_email_variations(generated_text):
    """Parse generated text into variation objects"""
    variations = []

    parts = generated_text.split('VARIATION')

    for i, part in enumerate(parts[1:], 1):
        if i > 2:
            break

        lines = part.strip().split('\n')
        subject = ""
        body_lines = []
        body_started = False

        for line in lines:
            line = line.strip()
            if line.upper().startswith('SUBJECT:'):
                subject = line[8:].strip()
            elif line.upper().startswith('BODY:'):
                body_started = True
            elif body_started:
                body_lines.append(line)

        body = '\n'.join(body_lines).strip()

        if subject and body:
            variations.append({
                'subject': subject,
                'body': body
            })

    if len(variations) < 2:
        logging.warning("Less than two variations parsed from AI response. Using fallback variations.")
        variations = [
            {
                'subject': 'Exclusive Offer Inside 🎯',
                'body': 'We have something special for you...\n\n[Learn More]'
            },
            {
                'subject': 'You\'re Going to Love This',
                'body': 'This is exactly what you\'ve been waiting for...\n\n[Discover More]'
            }
        ]

    return variations

# --- Finalize Mails Endpoints ---
@app.route('/list-template-categories')
def list_template_categories():
    base_dir = os.path.join(os.getcwd(), 'html_templates')
    categories = []
    if os.path.exists(base_dir) and os.path.isdir(base_dir):
        categories = [d for d in os.listdir(base_dir) if os.path.isdir(os.path.join(base_dir, d))]
        logging.info(f"Found template categories: {categories}")
    else:
        logging.warning(f"HTML templates base directory not found: {base_dir}")
    return jsonify({'success': True, 'categories': categories})

@app.route('/list-template-files/<category>')
def list_template_files(category):
    base_dir = os.path.join(os.getcwd(), 'html_templates', category)
    if not os.path.exists(base_dir):
        logging.warning(f"Template category directory not found: {base_dir}")
        return jsonify({'success': False, 'error': 'Category not found'})
    files = [f for f in os.listdir(base_dir) if f.endswith('.html')]
    logging.info(f"Found template files for category '{category}': {files}")
    return jsonify({'success': True, 'files': files})

@app.route('/get-template-content/<category>/<filename>')
def get_template_content(category, filename):
    base_dir = os.path.join(os.getcwd(), 'html_templates', category)
    file_path = os.path.join(base_dir, filename)
    if not os.path.exists(file_path):
        logging.warning(f"Template file not found: {file_path}")
        return jsonify({'success': False, 'error': 'Template not found'})
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    logging.info(f"Loaded content for template: {filename} in category {category}")
    return jsonify({'success': True, 'content': content})

@app.route('/get-campaign-variants/<campaign_id>')
def get_campaign_variants(campaign_id):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(sql.SQL('SELECT variation_name, subject_line, email_body FROM email_variations WHERE campaign_id = %s'), [campaign_id])
        variants = [{'name': row[0], 'subject': row[1], 'body': row[2]} for row in cursor.fetchall()]
        cursor.close()
        logging.info(f"Retrieved {len(variants)} variants for campaign {campaign_id}.")
        return jsonify({'success': True, 'variants': variants})
    except Exception as e:
        logging.error(f"Error in get_campaign_variants for {campaign_id}: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)})
    finally:
        if conn:
            conn.close()

@app.route('/unsubscribe/<tracking_id>')
def unsubscribe(tracking_id):
    """Handle unsubscribe requests"""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(sql.SQL('''
            UPDATE recipients
            SET status = 'unsubscribed', converted_at = CURRENT_TIMESTAMP -- Using converted_at to mark unsubscribe time, or add a dedicated column
            WHERE tracking_id = %s
        '''), [tracking_id])
        conn.commit()
        logging.info(f"Recipient {tracking_id} unsubscribed.")
        # Render a simple confirmation page or redirect to a success page
        return "You have successfully unsubscribed from our emails."
    except Exception as e:
        logging.error(f"Error unsubscribing {tracking_id}: {e}")
        traceback.print_exc()
        return "An error occurred during unsubscribe. Please try again or contact support."
    finally:
        if conn:
            conn.close()

@app.route('/integrate-content-template', methods=['POST'])
def integrate_content_template():
    data = request.get_json()
    content = data.get('content')
    template_html = data.get('template_html')

    if not content or not template_html:
        logging.warning("Missing content or template_html for integrate_content_template.")
        return jsonify({'success': False, 'error': 'Missing content or template_html'})

    groq_api_key = os.getenv('GROQ_API_KEY')
    if not groq_api_key:
        logging.error("GROQ_API_KEY not set in environment for integrate_content_template.")
        return jsonify({'success': False, 'error': 'GROQ_API_KEY not set in environment'})

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {groq_api_key}",
        "Content-Type": "application/json"
    }

    prompt = f"""You are an expert email formatter.
Integrate the following content into the provided HTML template.
Make sure to preserve styles and formatting.

CONTENT:
{content}

TEMPLATE_HTML:
{template_html}
"""

    payload = {
        "model": "llama3-70b-8192",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7
    }

    try:
        logging.info("Sending payload to Groq API...")
        # logging.debug(f"Payload sent to Groq: {json.dumps(payload, indent=2)}") # Uncomment for verbose payload logging

        response = requests.post(url, headers=headers, json=payload)
        # logging.debug(f"Raw Groq response: {response.text}") # Uncomment for verbose raw response logging

        response.raise_for_status() # Raise an HTTPError for bad responses (4xx or 5xx)

        result = response.json()
        raw_html = result['choices'][0]['message']['content']
        logging.info("Received response from Groq API.")

        # Step 1: Remove markdown-style HTML block markers
        if "```html" in raw_html:
            raw_html = raw_html.split("```html", 1)[-1]
        if "```" in raw_html:
            raw_html = raw_html.split("```", 1)[0]

        # Step 2: Strip typical AI wrap-up lines
        wrapup_phrases = [
        "Let me know if you need any further assistance",
        "Let me know if you need anything else",
        "Hope this helps",
        "Have a great day",
        "Happy to help"
        ]
        for phrase in wrapup_phrases:
            if phrase.lower() in raw_html.lower():
                raw_html = raw_html[:raw_html.lower().find(phrase.lower())].strip()

        # Step 3: Remove "Here is..." intro text
        raw_html = raw_html.strip()
        if raw_html.lower().startswith("here is"):
            raw_html = raw_html[raw_html.find("<"):]
        
        # Inline CSS
        finalized_html = transform(raw_html)
        logging.info("Groq response parsed and HTML transformed.")

        return jsonify({'success': True, 'finalized_html': finalized_html})

    except requests.exceptions.RequestException as req_e:
        logging.error(f"Request error with Groq API: {req_e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': f'Groq API request failed: {str(req_e)}'})
    except Exception as e:
        logging.error(f"Error parsing Groq response or transforming HTML: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': f'Processing error: {str(e)}'})

@app.route('/send-optimized-schedule', methods=['POST'])
def send_optimized_schedule():
    """Send finalized emails to customers based on open-time batches"""
    
    logging.info("📩 Starting optimized send route...")
    
    try:
        if 'customer_csv' not in request.files:
            logging.error("❌ No CSV file uploaded in request.files.")
            return jsonify({'success': False, 'error': 'CSV file not uploaded'})

        file = request.files['customer_csv']
        if file.filename == '':
            logging.error("❌ Empty filename received for customer_csv.")
            return jsonify({'success': False, 'error': 'No file selected'})

        subject = request.form.get('subject', '').strip()
        html_body_content = request.form.get('html_body', '').strip() # Renamed from html_body for clarity

        logging.info(f"✅ Subject: {subject[:50] if subject else 'None'}...")
        logging.info(f"✅ HTML body length: {len(html_body_content) if html_body_content else 0}")

        if not subject:
            logging.error("❌ Subject is required for optimized send.")
            return jsonify({'success': False, 'error': 'Subject is required'})
        if not html_body_content: # Check the new variable
            logging.error("❌ HTML body is required for optimized send.")
            return jsonify({'success': False, 'error': 'HTML body is required'})

        try:
            file_content = file.read()
            file.seek(0)
            
            encodings = ['utf-8', 'utf-8-sig', 'latin1', 'cp1252'] # <--- Improved CSV decoding attempts
            df = None
            
            for encoding in encodings:
                try:
                    file_string = file_content.decode(encoding)
                    df = pd.read_csv(io.StringIO(file_string))
                    logging.info(f"✅ CSV loaded successfully with encoding: {encoding}")
                    break
                except (UnicodeDecodeError, pd.errors.ParserError) as decode_error: # <--- Catching ParserError too
                    logging.debug(f"Attempting decode with {encoding} failed: {decode_error}")
                    continue
            
            if df is None:
                logging.error('❌ Could not read CSV file after trying multiple encodings.')
                return jsonify({'success': False, 'error': 'Could not read CSV file. Please check file encoding.'})
                
        except Exception as csv_error:
            logging.error(f"❌ Critical CSV reading error: {csv_error}")
            traceback.print_exc()
            return jsonify({'success': False, 'error': f'CSV reading error: {str(csv_error)}'})

        logging.info(f"📊 CSV Info: {len(df)} rows, columns: {list(df.columns)}")
        
        df_columns_lower = [col.lower().strip() for col in df.columns]
        email_col = None
        opentime_col = None
        
        for i, col in enumerate(df_columns_lower):
            if 'email' in col:
                email_col = df.columns[i]
            elif 'open' in col and 'time' in col:
                opentime_col = df.columns[i]
        
        if not email_col or not opentime_col:
            logging.error(f"❌ Required columns not found. Found: {list(df.columns)}. Need: email, opentime (case-insensitive, space-agnostic).")
            return jsonify({
                'success': False, 
                'error': f'Required columns not found. Found: {list(df.columns)}. Need: email, opentime'
            })

        logging.info(f"✅ Using columns: email='{email_col}', opentime='{opentime_col}'")

        df = df.dropna(subset=[email_col, opentime_col])
        df[email_col] = df[email_col].astype(str).str.strip()
        df[opentime_col] = df[opentime_col].astype(str).str.strip()
        
        df = df[(df[email_col] != '') & (df[opentime_col] != '')]
        
        logging.info(f"📊 After cleaning: {len(df)} valid rows for processing.")

        # Define batch times (simplified from your original to match your new logic)
        BATCH_SEND_TIMES = {
            "Morning Batch 1": (8, 0),
            "Morning Batch 2": (11, 0), 
            "Evening Batch 1": (14, 0),
            "Evening Batch 2": (19, 0),
            "Night Batch": (23, 0) # Consolidated Night Batch
        }

        try:
            service = authenticate_gmail()
            if not service:
                # This path should ideally be caught by the raise in authenticate_gmail()
                logging.error("❌ Gmail authentication failed - service object is None.")
                return jsonify({'success': False, 'error': 'Gmail authentication failed'})
            logging.info("✅ Gmail authenticated successfully.")
        except Exception as auth_error:
            logging.critical(f"❌ Gmail authentication error caught in route: {auth_error}")
            traceback.print_exc()
            return jsonify({'success': False, 'error': f'Gmail authentication error: {str(auth_error)}'})

        batches = {batch: [] for batch in BATCH_SEND_TIMES}
        processed_count = 0
        error_count = 0
        
        for index, row in df.iterrows():
            try:
                email = str(row[email_col]).strip()
                opentime_str = str(row[opentime_col]).strip()
                
                if '@' not in email or '.' not in email.split('@')[-1]:
                    logging.warning(f"⚠️ Invalid email format: '{email}' at row {index}.")
                    error_count += 1
                    continue
                
                try:
                    # Try HH:MM format first (e.g., "10:05")
                    if ':' in opentime_str:
                        open_time = datetime.datetime.strptime(opentime_str, "%H:%M").time()
                    # Try HH.MM format (e.g., "10.05") - less common for time, but good to cover
                    elif '.' in opentime_str:
                        open_time = datetime.datetime.strptime(opentime_str, "%H.%M").time()
                    else:
                        raise ValueError("Unknown time format for opentime.")
                        
                except ValueError as time_error:
                    logging.warning(f"⚠️ Invalid time format '{opentime_str}' for email '{email}' at row {index}: {time_error}")
                    error_count += 1
                    continue
                    
                hour = open_time.hour
                if 6 <= hour < 10:
                    batch = "Morning Batch 1"
                elif 10 <= hour < 12:
                    batch = "Morning Batch 2" 
                elif 12 <= hour < 17:
                    batch = "Evening Batch 1"
                elif 17 <= hour < 21:
                    batch = "Evening Batch 2"
                else:
                    batch = "Night Batch" # Catches 21:00-23:59 and 00:00-05:59
                    
                batches[batch].append({
                    'email': email,
                    'opentime': opentime_str,
                    'hour': hour
                })
                processed_count += 1
                
            except Exception as row_processing_error:
                logging.error(f"⚠️ Error processing row {index} for email '{row.get(email_col, 'N/A')}': {row_processing_error}")
                traceback.print_exc()
                error_count += 1
                continue

        logging.info(f"📊 Batch classification complete: {processed_count} valid entries, {error_count} errors/skipped.")

        batch_summary = []
        total_emails_to_send = 0
        
        for batch_name, emails_list in batches.items(): # Renamed 'emails' to 'emails_list' to avoid conflict with the 'emails' variable inside loop
            if emails_list:
                count = len(emails_list)
                send_hour, send_minute = BATCH_SEND_TIMES.get(batch_name, (0,0)) # Use .get() with default for safety
                batch_summary.append({
                    'batch': batch_name,
                    'count': count,
                    'send_time': f"{send_hour:02d}:{send_minute:02d}",
                    'emails_preview': [e['email'] for e in emails_list[:3]] # Show first 3 emails
                })
                total_emails_to_send += count

        if total_emails_to_send == 0:
            logging.warning('No valid emails found to process after classification.')
            return jsonify({
                'success': False,
                'error': 'No valid emails found to process for sending. Check your CSV and time formats.',
                'processed_count': processed_count,
                'error_count': error_count
            })

        logging.info(f"✅ Batch summary generated: {len(batch_summary)} unique batches, {total_emails_to_send} total emails ready for sending.")

        all_results = []
        
        for batch_name, emails_to_send in batches.items(): # Renamed 'emails' to 'emails_to_send'
            if not emails_to_send:
                continue
                
            logging.info(f"\n📤 Processing batch '{batch_name}' with {len(emails_to_send)} emails.")
            batch_results = {
                'batch': batch_name,
                'total': len(emails_to_send),
                'sent': 0,
                'failed': 0,
                'errors': []
            }
            
            for email_data in emails_to_send:
                try:
                    email_address = email_data['email'] # Renamed from 'email' to avoid conflict
                    
                    # Ensure the html_body_content is passed as the body for both plain and HTML parts
                    msg = create_email_message(
                        to_email=email_address,
                        subject=subject,
                        body=html_body_content, # <--- THIS IS THE CRITICAL CHANGE: Pass the full HTML content
                        tracking_id=str(uuid.uuid4())
                    )
                    
                    if msg is None: # Check if create_email_message returned None due to internal error
                        logging.error(f"❌ Message creation failed for {email_address}. Skipping send.")
                        batch_results['failed'] += 1
                        batch_results['errors'].append(f"{email_address}: Message creation failed.")
                        continue

                    result = send_email_via_gmail(service, msg)
                    
                    if result and result.get('success'):
                        logging.info(f"✅ Sent to {email_address}")
                        batch_results['sent'] += 1
                    else:
                        error_msg = result.get('error', 'Unknown error during send') if result else 'No send result'
                        logging.error(f"❌ Failed to send to {email_address}: {error_msg}")
                        batch_results['failed'] += 1
                        batch_results['errors'].append(f"{email_address}: {error_msg}")
                        
                except Exception as send_loop_error:
                    logging.error(f"❌ Exception in send loop for {email_data['email']}: {send_loop_error}")
                    traceback.print_exc()
                    batch_results['failed'] += 1
                    batch_results['errors'].append(f"{email_data['email']}: {str(send_loop_error)}")
            
            all_results.append(batch_results)
            
            if len(emails_to_send) > 10:
                time.sleep(1) # Small delay for larger batches

        final_total_sent = sum(r['sent'] for r in all_results)
        final_total_failed = sum(r['failed'] for r in all_results)

        success_response = {
            'success': True,
            'message': f'Email campaign processing completed. Sent {final_total_sent} emails, Failed {final_total_failed}.',
            'summary': {
                'total_processed_rows_from_csv': processed_count,
                'total_csv_parse_errors_skipped': error_count,
                'total_emails_attempted_send': total_emails_to_send,
                'total_sent_successfully': final_total_sent,
                'total_failed_to_send': final_total_failed,
                'batches_processed': len([r for r in all_results if r['total'] > 0])
            },
            'batch_results': all_results,
            'batch_summary': batch_summary
        }
        
        logging.info("✅ Send optimized schedule completed successfully.")
        return jsonify(success_response)
        
    except Exception as e:
        error_msg = f"Critical and unexpected error in send_optimized_schedule route: {str(e)}"
        logging.critical(f"❌ {error_msg}")
        traceback.print_exc() # This is the most important for you to see the full error
        return jsonify({'success': False, 'error': error_msg})


if __name__ == '__main__':
    logging.info("🧪 A/B Testing Email Marketing App Starting...")
    logging.info("✉️ Gmail API Integration Ready")
    logging.info("📊 Campaign Tracking Enabled")
    logging.info("🎯 Endpoints:")
    logging.info(f"   - Main: {BASE_URL}")
    logging.info(f"   - Dashboard: {BASE_URL}/ab-dashboard")
    logging.info(f"   - Campaigns: {BASE_URL}/campaigns")

    app.run(debug=True, host='0.0.0.0', port=PORT)
