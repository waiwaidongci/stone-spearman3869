from fastapi import APIRouter, Depends
from fastapi.responses import Response
from src.config.database import get_db
from src.middlewares.auth import current_user, admin
from src.schemas.privacy import PrivacyReleaseInput
from src.controllers import privacy as controller

router = APIRouter()


@router.get('/privacy/budget')
def budget(db=Depends(get_db), user=Depends(current_user)):
    return controller.budget(db)


@router.post('/privacy/releases')
def create_release(data: PrivacyReleaseInput, db=Depends(get_db), user=Depends(current_user)):
    # 所有登录角色均可申请；申请即按纪律扣费。
    return controller.create_release(db, data, user)


@router.get('/privacy/releases')
def list_releases(db=Depends(get_db), user=Depends(current_user)):
    return controller.list_releases(db)


@router.get('/privacy/releases/{release_id}')
def get_release(release_id: int, db=Depends(get_db), user=Depends(current_user)):
    return controller.get_release(db, release_id)


@router.post('/privacy/releases/{release_id}/retry')
def retry_release(release_id: int, db=Depends(get_db), user=Depends(current_user)):
    return controller.retry_release(db, release_id, user)


@router.post('/privacy/releases/{release_id}/publish')
def publish(release_id: int, db=Depends(get_db), user=Depends(admin)):
    return controller.publish(db, release_id, user)


@router.post('/privacy/releases/{release_id}/revoke')
def revoke(release_id: int, db=Depends(get_db), user=Depends(admin)):
    return controller.revoke(db, release_id, user)


@router.get('/privacy/releases/{release_id}/export')
def export(release_id: int, db=Depends(get_db), user=Depends(current_user)):
    return Response(
        content=controller.export_public(db, release_id, user).encode('utf-8'),
        media_type='text/csv',
        headers={'Content-Disposition': f'attachment; filename="privacy-release-{release_id}.csv"'})
