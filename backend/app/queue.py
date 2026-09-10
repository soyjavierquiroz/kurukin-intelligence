"""RabbitMQ carries IDs only. Never log connection parameters or broker exceptions."""
import json
from .config import get_settings

QUEUE = 'transcription.v1'


def connect():
    import pika
    parameters = pika.URLParameters(get_settings().resolve_rabbitmq_url())
    parameters.socket_timeout = 5
    parameters.stack_timeout = 10
    parameters.blocked_connection_timeout = 5
    parameters.connection_attempts = 1
    parameters.heartbeat = 30
    return pika.BlockingConnection(parameters)


def declare(channel):
    channel.queue_declare(queue=QUEUE, durable=True, arguments={'x-max-priority': 100})


def publish_job(job):
    import pika
    connection = connect()
    try:
        channel = connection.channel()
        declare(channel)
        channel.confirm_delivery()
        channel.basic_publish(exchange='', routing_key=QUEUE,
            body=json.dumps({'job_id': str(job.id), 'video_id': str(job.video_id)}).encode(),
            properties=pika.BasicProperties(content_type='application/json', delivery_mode=2,
                                            priority=max(0, min(100, job.priority))), mandatory=True)
    finally:
        connection.close()
