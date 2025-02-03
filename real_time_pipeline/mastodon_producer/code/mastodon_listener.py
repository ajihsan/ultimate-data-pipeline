from mastodon import Mastodon, StreamListener
from bs4 import BeautifulSoup
import argparse
import datetime
from threading import Timer
import os
import json
from dotenv import load_dotenv

from kafka_producer import kafka_producer

# Load environment variables from the .env file (if present)
load_dotenv()

# Access environment variables as if they came from the actual environment
CLIENT_ID = os.getenv('CLIENT_ID')
CLIENT_SECRET = os.getenv('CLIENT_SECRET')
ACCESS_TOKEN = os.getenv('ACCESS_TOKEN')

# globals
base_url = ''
enable_kafka = False
quiet = False
watchdog = False
topic_name, producer = '' , ''


# Listener for Mastodon events
class Listener(StreamListener):

    def on_update(self, status):
        if watchdog:
            # reset watchdog timer
            watchdog.reset()

        m_text = BeautifulSoup(status.content, 'html.parser').text
        num_tags = len(status.tags)
        num_chars = len(m_text)
        num_words = len(m_text.split())
        m_lang = status.language
        if m_lang is None:
            m_lang = 'unknown'
        m_user = status.account.username

        app=''
        # attribute only available on local
        if hasattr(status, 'application'):
            app = status.application.get('name')

        now_dt=datetime.datetime.now()
        
        value_dict = { 
            'm_id': status.id,
            'created_at': now_dt.strftime('%Y-%m-%d %H:%M:%S'),
            'app': app,
            'url': status.url,
            'base_url': base_url,  
            'language': m_lang, 
            'favourites': status.favourites_count, 
            'username': m_user, 
            'bot': status.account.bot, 
            'tags': num_tags, 
            'characters': num_chars, 
            'words': num_words, 
            'mastodon_text': m_text
        }

        if not quiet:
            print(f'{m_user} {m_lang}', m_text[:30])


        if enable_kafka:
            producer.produce(topic = topic_name, value = json.dumps(value_dict))
            producer.flush()


class Watchdog:
    def __init__(self, timeout, userHandler=None): # timeout in seconds
        self.timeout = timeout
        if userHandler != None:
            self.timer = Timer(self.timeout, userHandler)
        else:
            self.timer = Timer(self.timeout, self.handler)

    def reset(self):
        self.timer.cancel()
        self.timer = Timer(self.timeout, watchExpired)
        self.timer.start()

    def stop(self):
        self.timer.cancel()

    def handler(self):
        raise self


def watchExpired():
    print('Watchdog expired')
    # ugly, but expected method for a child process to terminate a fork
    os._exit(1)


def main():
    global base_url
    global enable_kafka
    global quiet
    global watchdog
    global topic_name, producer

    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    parser.add_argument(
        '--enableKafka',
        help='Whether to enable Kafka producer.',
        action='store_true',
        required=False,
        default=False)

    parser.add_argument(
        '--public',
        help='listen to public stream (instead of local).',
        action='store_true',
        required=False,
        default=False)

    parser.add_argument(
        '--watchdog',
        help='enable watchdog timer of n seconds',
        type=int,
        required=False)

    parser.add_argument(
        '--quiet',
        help='Do not echo a summary of the toot',
        action='store_true',
        required=False,
        default=False)

    parser.add_argument(
        '--baseURL',
        help='Server URL',
        required=False,
        default='https://mastodon.social')      

    args = parser.parse_args()

    base_url=args.baseURL
    enable_kafka=args.enableKafka

    if enable_kafka:
        topic_name, producer = kafka_producer()

    mastodon = Mastodon(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, access_token=ACCESS_TOKEN, api_base_url = "https://mastodon.social")

    if args.watchdog:
        watchdog = Watchdog(args.watchdog, watchExpired)
        watchdog.timer.start()

    if args.public:
        mastodon.stream_public(Listener())
    else:
        mastodon.stream_local(Listener())

if __name__ == '__main__':
    main()