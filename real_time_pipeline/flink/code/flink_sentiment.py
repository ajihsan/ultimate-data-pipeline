from pyflink.datastream import StreamExecutionEnvironment
from pyflink.datastream.connectors import FlinkKafkaProducer, FlinkKafkaConsumer
from pyflink.datastream.functions import MapFunction
from pyflink.common.serialization import SimpleStringSchema
from pyflink.common import WatermarkStrategy, Types
import json
import requests

# Implement the custom SentimentAnalysis map function
class SentimentAnalysis(MapFunction):
    def map(self, value):
        # Parse input JSON
        message_data = json.loads(value)
        text_to_analyze = message_data.get("mastodon_text", "")

        if not text_to_analyze:
            print("No mastodon_text found in message")
            return
        
        def process_response(response: json):
            try:
                response_json = response.json()
                response_text = response_json["response"].split("</think>\n\n")[-1].upper()
                return response_text
            except Exception as e:
                print(f"Error processing response: {str(e)}")
        
        def make_request():
            url = "http://ollama:11434/api/generate"
            headers = {
                "Content-Type": "application/json"
            }
            payload = self.generate_prompt(text_to_analyze)
            return requests.post(url, headers=headers, json=payload, timeout=100)
        
        response = make_request()
        sentiment_result = process_response(response)
        message_data['sentiment'] = sentiment_result
        print(message_data)

        return json.dumps(message_data)
    
    def generate_prompt(self, input_text):
        user_message = f"""
        Your task is to process a social media message delimited by ---.
        Process the message and give me this information divided by symbol ; Don't use spaces or other punctuations. Use only this order!
        First is sentiment analysis. Specify only using one of the words POSITIVE, NEGATIVE, UNKNOWN, NEUTRAL. Don't use other words.
        Second, identify the language of the message: EN, RU, FR, UA, etc. Use UpperCase, if cannot define use UNKNOWN. Don't use other words.
        Third, is there sarcasm in the message. use only these options: HAS SARCASM, NO SARCASM. If cannot define use UNKNOWN. Don't use other words.
        Fourth, closest category of the message: News, Politics, Entertainment, Sports, Technology, Health, Culture, Travel, Fashion, Beauty, Food. Don't use other words.
        Fifth, if it contains any offensive, rude, inappropriate language, or sex language. Use these words: INAPPROPRIATE, APPROPRIATE. Don't use other words.
        For example:
        - It is amazing my favourite sport team has won!, you should return POSITIVE;EN;NO SARCASM;Sports;APPROPRIATE
        - I'm so angry about this new law!, you should return NEGATIVE;EN;NO SARCASM;Politics;APPROPRIATE
        Answer with only a set of words in the right order described above, no additional description. Spell words correctly.
        ---
        {input_text}
        ---"""
        
        return {
            "model": "deepseek-r1:1.5b",
            "prompt":user_message,
            "stream": False
        }
        
# class Message:
#     def __init__(self, original, processed):
#         self.original = original
#         self.processed = processed
    
#     def transform(self):
#         self.original['sentiment'] = self.processed
#         return json.dumps(self.original['sentiment'])

# class AsyncHttpRequestFunction():
#     def __init__(self):
#         super().__init__()
#         self.executor = ThreadPoolExecutor(max_workers=4)

#     def async_invoke(self, input_str, result_future: ResultFuture):
#         try:
#             # Parse input JSON
#             message_data = json.loads(input_str)
#             text_to_analyze = message_data.get("mastodon_text", "")
            
#             if not text_to_analyze:
#                 print("No mastodon_text found in message")
#                 result_future.complete([])
#                 return
            
#             def process_response(future):
#                 try:
#                     response = future.result()
#                     response_json = response.json()
#                     response_text = response_json["response"].split("</think>\n\n")[-1].upper()
#                     message = Message(message_data, response_text)
#                     result_future.complete([message])
#                 except Exception as e:
#                     print(f"Error processing response: {str(e)}")

#             def make_request():
#                 url = "http://localhost:11434/api/generate"
#                 headers = {
#                     "Content-Type": "application/json"
#                 }
#                 payload = self.generate_prompt(text_to_analyze)
#                 return requests.post(url, headers=headers, json=payload, timeout=10)

#             future = self.executor.submit(make_request)
#             future.add_done_callback(process_response)

#         except json.JSONDecodeError:
#             print(f"Invalid JSON received: {input_str}")
#             result_future.complete([])

#     def generate_prompt(self, input_text):
#         user_message = f"""
#         Your task is to process a social media message delimited by ---.
#         Process the message and give me this information divided by symbol ; Don't use spaces or other punctuations. Use only this order!
#         First is sentiment analysis. Specify only using one of the words POSITIVE, NEGATIVE, UNKNOWN, NEUTRAL. Don't use other words.
#         Second, identify the language of the message: EN, RU, FR, UA, etc. Use UpperCase, if cannot define use UNKNOWN. Don't use other words.
#         Third, is there sarcasm in the message. use only these options: HAS SARCASM, NO SARCASM. If cannot define use UNKNOWN. Don't use other words.
#         Fourth, closest category of the message: News, Politics, Entertainment, Sports, Technology, Health, Culture, Travel, Fashion, Beauty, Food. Don't use other words.
#         Fifth, if it contains any offensive, rude, inappropriate language, or sex language. Use these words: INAPPROPRIATE, APPROPRIATE. Don't use other words.
#         For example:
#         - It is amazing my favourite sport team has won!, you should return POSITIVE;EN;NO SARCASM;Sports;APPROPRIATE
#         - I'm so angry about this new law!, you should return NEGATIVE;EN;NO SARCASM;Politics;APPROPRIATE
#         Answer with only a set of words in the right order described above, no additional description. Spell words correctly.
#         ---
#         {input_text}
#         ---"""
        
#         return {
#             "model": "deepseek-r1:1.5b",
#             "prompt":user_message,
#             "stream": False
#         }

def main():
    env = StreamExecutionEnvironment.get_execution_environment()
    env.set_parallelism(1)
    env.add_jars("file:///home/pyflink/flink-sql-connector-kafka_2.11-1.14.0.jar")

    # Configure Kafka
    kafka_props = {
        'bootstrap.servers': "kafka:29092",
        'auto.offset.reset': 'earliest'
    }

    # Create Kafka source
    consumer = FlinkKafkaConsumer(
        topics="mastodon-topic",
        deserialization_schema=SimpleStringSchema(),
        properties=kafka_props
    )

    # Define a source to read from Kafka
    # consumer = KafkaSource.builder() \
    #     .set_bootstrap_servers("localhost:9092") \
    #     .set_topics("mastodon-topic") \
    #     .set_group_id("analysis_group") \
    #     .set_starting_offsets(KafkaOffsetsInitializer.earliest()) \
    #     .set_value_only_deserializer(SimpleStringSchema()) \
    #     .build()

    # Create Kafka sink
    producer = FlinkKafkaProducer(
        topic="mastodon_sentiment",
        serialization_schema=SimpleStringSchema(),
        producer_config=kafka_props
    )

    # Build pipeline
    stream = env.add_source(consumer, "Kafka Source")
    stream = stream.assign_timestamps_and_watermarks(WatermarkStrategy.for_monotonous_timestamps())

    processed_stream = stream.map(SentimentAnalysis(), output_type=Types.STRING()).name("Sentiment Analysis")

    # processed_stream = AsyncWaitOperator.unordered_wait(
    #     stream,
    #     AsyncHttpRequestFunction(),
    #     10000,  # timeout
    #     100     # capacity
    # )

    # processed_stream.map(lambda msg: msg.transform()).add_sink(producer)

    processed_stream.add_sink(producer).name("Kafka Sink")

    env.execute("Kafka to Kafka Job")

if __name__ == '__main__':
    main()