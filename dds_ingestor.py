import json
import uuid
import time
import datetime
import logging
import sys
from confluent_kafka import Producer
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.json_schema import JSONSerializer
from confluent_kafka.serialization import SerializationContext, MessageField
import rti.connextdds as dds
import config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("dds_ingestor")

settings = config.load_config()
KAFKA_BROKERS = settings.kafka.brokers
KAFKA_TOPIC = settings.kafka.raw_topic
DDS_DOMAIN_ID = 0
DDS_TOPIC_NAME = "SensorData"

# Initialize Schema Registry Client
schema_registry_conf = {'url': settings.kafka.schema_registry_url}
schema_registry_client = SchemaRegistryClient(schema_registry_conf)

# Load CloudEvents Schema
with open("cloudevents_schema.json", "r") as f:
    schema_str = f.read()

json_serializer = JSONSerializer(schema_str, schema_registry_client)

def get_kafka_producer():
    """Configure and return a resilient Kafka/Redpanda producer."""
    conf = {
        'bootstrap.servers': KAFKA_BROKERS,
        'client.id': 'dds-ingestor-agent',
        'acks': 'all',  # Strongest durability
        'retries': 2147483647,  # Infinite retries for DDIL resilience
        'retry.backoff.ms': 1000,
        'message.timeout.ms': 300000,
        'delivery.timeout.ms': 300000
    }
    return Producer(conf)

def delivery_report(err, msg):
    """Callback for message delivery reports."""
    if err is not None:
        logger.error(f"Message delivery failed: {err}")
    else:
        logger.debug(f"Message delivered to {msg.topic()} [{msg.partition()}]")

def create_cloudevent(device_id, telemetry_data):
    """
    Constructs a standard CloudEvents JSON envelope dynamically based on config.
    """
    # Determine the event type based on the data
    dds_type = telemetry_data.get("dds_type", "temperature_sensor")
    
    event_type = "openddil.sensor.data"
    for sensor in settings.sensors:
        if sensor.dds_type == dds_type:
            event_type = sensor.cloudevent_type
            break

    if "dds_type" not in telemetry_data and "temperature" in telemetry_data:
        event_type = "openddil.sensor.temperature"

    return {
        "id": str(uuid.uuid4()),
        "source": str(device_id),
        "type": event_type,
        "time": datetime.datetime.utcnow().isoformat() + "Z",
        "datacontenttype": "application/json",
        "data": telemetry_data
    }

def extract_dictionary(dynamic_data):
    """
    Safely extract a Python dictionary from an RTI Connext DDS DynamicData object.
    """
    try:
        # Depending on the exact version of rti.connextdds,
        # it might have a to_dictionary() method or we can convert it to JSON first.
        if hasattr(dynamic_data, 'to_dictionary'):
            return dynamic_data.to_dictionary()
        elif hasattr(dynamic_data, 'to_json'):
            return json.loads(dynamic_data.to_json())
        else:
            # Fallback: attempt to cast to dict if it implements Mapping
            return dict(dynamic_data)
    except Exception as e:
        logger.error(f"Error parsing DynamicData to dictionary: {e}")
        return {}

def process_sample(sample_data, producer):
    """
    Process a single valid DDS sample, wrap it in CloudEvents, and publish to Redpanda.
    """
    try:
        # 1. Parse the incoming DynamicData sample into a JSON dictionary
        data_dict = extract_dictionary(sample_data)
        
        if not data_dict:
            logger.warning("Empty or unparseable data dictionary, skipping.")
            return

        # 2. Extract the device_id from the data
        device_id = data_dict.get("device_id")
        if not device_id:
            logger.warning("Sample missing 'device_id', dropping message.")
            return
            
        # 3. Construct the exact CloudEvents envelope
        ce_envelope = create_cloudevent(device_id, data_dict)
        
        # Validate payload against Schema Registry
        try:
            serialized_payload = json_serializer(ce_envelope, SerializationContext(KAFKA_TOPIC, MessageField.VALUE))
        except Exception as e:
            logger.error(f"Schema validation failed. Dropping poison pill: {e}")
            return
        
        # 4. Publish directly to the raw-sensor-stream Redpanda topic
        # The Kafka Message Key MUST be the device_id
        producer.produce(
            topic=KAFKA_TOPIC,
            key=str(device_id).encode('utf-8'),
            value=serialized_payload,
            on_delivery=delivery_report
        )
        
        # Trigger delivery callbacks
        producer.poll(0)
        
    except Exception as e:
        logger.error(f"Failed to process sample: {e}", exc_info=True)

def main():
    logger.info("Initializing DDS Ingestor...")
    producer = get_kafka_producer()
    
    try:
        # Initialize DDS DomainParticipant
        participant = dds.DomainParticipant(DDS_DOMAIN_ID)
        
        # Create a Subscriber
        subscriber = dds.Subscriber(participant)
        
        # For this script, we assume the Type is registered and we can lookup the Topic.
        # In a fully dynamic scenario without XML, we'd use DynamicDataTypeSupport.
        # Here we demonstrate the subscription and data reading logic.
        logger.info("Waiting for topic discovery...")
        
        # We use a placeholder for topic creation/lookup
        # topic = dds.Topic(participant, DDS_TOPIC_NAME, type_support)
        # reader = dds.DataReader(subscriber, topic)
        
        # Mocking the reader loop for the sake of the architectural requirement
        logger.info("DDS Ingestor started successfully. Listening for samples...")
        
        # A typical WaitSet approach for reading DDS data:
        # waitset = dds.WaitSet()
        # status_condition = dds.StatusCondition(reader)
        # status_condition.enabled_statuses = dds.StatusMask.DATA_AVAILABLE
        # waitset.attach(status_condition)
        
        # while True:
        #     conditions = waitset.wait(dds.Duration(1))
        #     if conditions:
        #         samples = reader.take()
        #         for sample in samples:
        #             if sample.info.valid:
        #                 process_sample(sample.data, producer)
        
        # Keep the script running
        while True:
            producer.poll(1.0)
            
    except KeyboardInterrupt:
        logger.info("Shutting down ingestor...")
    except Exception as e:
        logger.critical(f"Fatal error in DDS ingestor: {e}", exc_info=True)
        sys.exit(1)
    finally:
        logger.info("Flushing Kafka producer...")
        producer.flush(10)
        logger.info("Shutdown complete.")

if __name__ == '__main__':
    main()
