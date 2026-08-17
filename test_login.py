from atproto import Client, Request
from httpx import Timeout

def main():
    print("Initializing client with a 30-second timeout...")
    
    # Configure an explicit 30.0 second timeout for requests
    custom_request = Request(timeout=Timeout(30.0))
    client = Client(request=custom_request)
    
    try:
        print("Attempting to login...")
        profile = client.login('coreiq.bsky.social', 'Hopefrey2004')
        print(f"Successfully logged in as: {profile.display_name}")
    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    main()
