from rest_framework import serializers
from .models import User, UserPreference
from django.contrib.auth import authenticate

class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only = True)

    class Meta:
        model = User

        fields = [
            "id",
            "username",
            "email",
            "password",
            "first_name",
            "last_name",
        ]

    def create(self, validated_data):
        password = validated_data.pop("password")

        user = User(**validated_data)

        user.set_password(password)

        user.save()

        return user
    

class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        email = attrs.get("email")
        password = attrs.get("password")

        user = authenticate(
            username=email,
            password=password
        )

        if user is None:
            raise serializers.ValidationError(
                "Incorrect Email or Password"
            )
        
        attrs["user"] = user
        return attrs


class UserPreferenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserPreference
        fields = (
            "pnl_alerts",
            "trade_execution_alerts",
            "copy_message_notifications",
            "copy_signal_notifications",
            "system_alerts",
            "updated_at",
        )
        read_only_fields = ("updated_at",)


class UserProfileSerializer(serializers.ModelSerializer):
    preferences = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ("id", "username", "email", "first_name", "last_name", "preferences")
        read_only_fields = ("id", "email")

    def get_preferences(self, obj):
        preferences, _ = UserPreference.objects.get_or_create(user=obj)
        return UserPreferenceSerializer(preferences).data

    def update(self, instance, validated_data):
        preferences_data = self.context["request"].data.get("preferences")
        instance = super().update(instance, validated_data)
        if preferences_data is not None:
            preferences, _ = UserPreference.objects.get_or_create(user=instance)
            serializer = UserPreferenceSerializer(
                preferences,
                data=preferences_data,
                partial=True,
            )
            serializer.is_valid(raise_exception=True)
            serializer.save()
        return instance
