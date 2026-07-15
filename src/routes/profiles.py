from pathlib import Path
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import APIRouter, Depends, status, HTTPException
from database import (
    get_db,
    UserModel,
    UserGroupEnum,
    UserProfileModel
)
from exceptions import BaseSecurityError, TokenExpiredError, S3FileUploadError
from schemas.profiles import ProfileResponseSchema, ProfileRequestSchema
from security.interfaces import JWTAuthManagerInterface
from config import get_jwt_auth_manager, get_s3_storage_client
from security.http import get_token
from sqlalchemy.orm import joinedload

from storages import S3StorageInterface

router = APIRouter()


@router.post(
    "/users/{user_id}/profile/",
    response_model=ProfileResponseSchema,
    status_code=status.HTTP_201_CREATED,
)
async def create_user_profile(
        user_id: int,
        profile: ProfileRequestSchema = Depends(ProfileRequestSchema.as_form),
        db: AsyncSession = Depends(get_db),
        token: str = Depends(get_token),
        jwt_manager: JWTAuthManagerInterface = Depends(get_jwt_auth_manager),
        s3_storage_client: S3StorageInterface = Depends(get_s3_storage_client),
):
    try:
        decoded_token = jwt_manager.decode_access_token(token)
    except TokenExpiredError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired.",
        )
    except BaseSecurityError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization header format. Expected 'Bearer <token>'",
        )
    result = await db.execute(
        select(UserModel)
        .where(UserModel.id == decoded_token["user_id"])
        .options(
            joinedload(UserModel.group),
        )
    )
    db_user_from_token = result.scalar_one_or_none()
    result = await db.execute(
        select(UserModel)
        .where(UserModel.id == user_id)
        .options(
            joinedload(UserModel.group),
            joinedload(UserModel.profile),
        )
    )
    db_user = result.scalar_one_or_none()
    if not db_user or not db_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or not active.",
        )
    if db_user_from_token is None or not db_user_from_token or not db_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or not active.",
        )
    if db_user.profile:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User already has a profile.",
        )
    if db_user_from_token.group.name == UserGroupEnum.ADMIN or decoded_token["user_id"] == user_id:
        avatar = profile.avatar

        extension = Path(avatar.filename).suffix  # ".jpg"
        file_name = f"avatars/{db_user.id}_avatar{extension}"

        file_data = await avatar.read()

        try:
            await s3_storage_client.upload_file(file_name, file_data)
        except S3FileUploadError:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to upload avatar. Please try again later."
            )
        file_url = await s3_storage_client.get_file_url(file_name)

        user_profile = UserProfileModel(
            first_name=profile.first_name,
            last_name=profile.last_name,
            gender=profile.gender,
            date_of_birth=profile.date_of_birth,
            info=profile.info,
            avatar=file_name,
            user_id=db_user.id
        )
        db.add(user_profile)
        await db.flush()
    else:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to edit this profile.",
        )
    await db.commit()
    await db.refresh(user_profile)

    return {
        "id": user_profile.id,
        "user_id": db_user.id,
        "first_name": user_profile.first_name,
        "last_name": user_profile.last_name,
        "gender": user_profile.gender,
        "date_of_birth": user_profile.date_of_birth,
        "info": user_profile.info,
        "avatar": file_url,
    }
