from pyflink.common.serialization import SimpleStringSchema
from pyflink.common.watermark_strategy import WatermarkStrategy
from pyflink.datastream.connectors import KafkaSource, KafkaSink, KafkaRecordSerializationSchema, DeliveryGuarantee, KafkaOffsetsInitializer
from pyflink.datastream import StreamExecutionEnvironment
from pyflink.datastream.execution_mode import RuntimeExecutionMode
import requests

def sentiment_analysis_job():
    """
    Sets up a PyFlink job that consumes social media posts from a Kafka topic, performs sentiment analysis, aggregates results in 5-minute windows, and sends the output to another Kafka topic.
    """
    # Declare the execution environment
    env = StreamExecutionEnvironment.get_execution_environment()
    env.set_parallelism(1)
    env.set_runtime_mode(RuntimeExecutionMode.STREAMING)

    # Define a source to read from Kafka
    source = KafkaSource.builder() \
        .set_bootstrap_servers("localhost:9092") \
        .set_topics("mastodon-topic") \
        .set_starting_offsets(KafkaOffsetsInitializer.earliest()) \
        .set_value_only_deserializer(SimpleStringSchema()) \
        .build()

    ds = env.from_source(source, WatermarkStrategy.for_monotonous_timestamps(), "Kafka Source")

    # Define processing logic
    
    # Define a sink to write to Kafka
    sink = KafkaSink.builder() \
        .set_bootstrap_servers("localhost:9092") \
        .set_record_serializer(
            KafkaRecordSerializationSchema.builder()
                .set_topic("mastodon_sentiment_analysis)")
                .set_value_serialization_schema(SimpleStringSchema())
                .build()
        ) \
        .set_delivery_guarantee(DeliveryGuarantee.AT_LEAST_ONCE) \
        .build()

    message = "Your task is to process a social media message delimited by --- ."  \
    "Process the message and give me this information divided by symbol ; Don't use spaces or other punctuations. Use only this order!"  \
    "First is sentiment analysis. Specify only using one of the words POSITIVE, NEGATIVE, UNKNOWN, NEUTRAL. Don't use other words."  \
    "Second, identify the language of the message: EN, RU, FR, UA, etc. Use UpperCase, if cannot define use UNKNOWN. Don't use other words. "  \
    "Third, is there sarcasm in the message. use only these options: HAS SARCASM, NO SARCASM. If cannot define use UNKNOWN. Don't use other words. "  \
    "Fourth, closest category of the message: News, Politics, Entertainment, Sports, Technology, Health, Culture, Travel, Fashion, Beauty, Food. Don't use other words."  \
    "Fifth, if it contains any offensive, rude, inappropriate language, or sex language. Use these words: INAPPROPRIATE, APPROPRIATE. Don't use other words. "  \
    "For example:"  \
    "- It is amazing my favourite sport team has won!, you should return POSITIVE;EN;NO SARCASM;Sports;APPROPRIATE"  \
    "- I'm so angry about this new law!, you should return NEGATIVE;EN;NO SARCASM;Politics;APPROPRIATE"  \
    "Answer with only a set of words in the right order described above, no additional description. Spell words correctly."  \
    "--- "
    content = ""
    final_message = message+content

    # # Direct the processed data to the sink
    # windowed_stream.sink_to(sink)

    # Execute the job
    env.execute("Kafka to Kafka Job")
    

def ask_model(question: str, model: str):
        """
        Sends a request to the model server and fetches a response.
        """
        url = "http://localhost:8000/v1/chat/completions"  # Adjust the URL if different
        headers = {"Content-Type": "application/json"}
        data = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": question
                }
            ]
        }

        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()  # Raise exception for HTTP errors
        return response.json()


# # Create Flink environment
# env = StreamExecutionEnvironment.get_execution_environment()
# t_env = StreamTableEnvironment.create(env)

# # Define Kafka source
# t_env.connect(
#     Kafka()
#     .version("universal")
#     .topic("mastodon-topic")
#     .start_from_latest()
#     .property("bootstrap.servers", "localhost:9092")
# ).with_format(
#     Json()
# ).with_schema(
#     Schema().field("field1", "STRING").field("field2", "INT")
# ).create_temporary_table("kafka_source")

# # Define processing logic
# result = t_env.from_path("kafka_source").select("field1, field2 1")

# # Write the result to a Kafka sink
# result.execute_insert("output_topic")

# # Execute Flink job
# env.execute("Python Flink Job")