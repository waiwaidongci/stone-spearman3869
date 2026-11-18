from src.services import privacy as privacy_service
from redis import Redis
import os


def budget(db):
    return privacy_service.budget_view(db)


def create_release(db, data, user):
    redis = Redis.from_url(os.environ['REDIS_URL'])
    return privacy_service.create_release(db, data, user, redis)


def list_releases(db):
    return privacy_service.list_releases(db)


def get_release(db, release_id):
    return privacy_service.get_release(db, release_id)


def retry_release(db, release_id, user):
    redis = Redis.from_url(os.environ['REDIS_URL'])
    return privacy_service.retry_release(db, release_id, user, redis)


def publish(db, release_id, user):
    return privacy_service.publish(db, release_id, user)


def revoke(db, release_id, user):
    return privacy_service.revoke(db, release_id, user)


def export_public(db, release_id, user):
    return privacy_service.export_public(db, release_id, user)
