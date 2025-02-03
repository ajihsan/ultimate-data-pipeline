import json
from confluent_kafka import Producer, KafkaException


def acked(err, msg):
    if err is not None:
        print('Failed to deliver message: %s: %s' % msg.value().decode('utf-8'), str(err))
    else:
        print('Message produced: %s' % msg.value().decode('utf-8'))


def kafka_producer():
    producer_config = {
        'bootstrap.servers': 'kafka:29092'
    }

    producer = Producer(producer_config)
    return 'mastodon-topic', producer

def main():
    # example test producer
    topic_name, producer = kafka_producer()

    value_dict = {  'language': 'en', 'favourites': 0, 'username': 'bob', 'bot': False, 'tags': 0, 'characters': 50, 'words': 12}
    producer.produce(topic = topic_name, value = value_dict)
    producer.flush()

if __name__ == '__main__':
    main()