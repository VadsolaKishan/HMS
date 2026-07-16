from rest_framework import viewsets, status, serializers
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db import transaction, IntegrityError
from django.utils import timezone
from .models import Appointment
from .serializers import AppointmentSerializer
from accounts.permissions import IsAdminOrStaff, IsAppointmentParticipant
from support.models import Notification
from accounts.models import User


class AppointmentViewSet(viewsets.ModelViewSet):
    serializer_class = AppointmentSerializer
    permission_classes = [IsAuthenticated, IsAppointmentParticipant]

    def get_permissions(self):
        """
        - list / upcoming: IsAuthenticated (queryset handles role filtering)
        - retrieve: IsAuthenticated + IsAppointmentParticipant (object-level)
        - create: IsAuthenticated (role logic in perform_create)
        - approve / reject: Admin only (checked inside the action)
        - cancel: checked inside the action (patient own + admin)
        - update / partial_update / destroy: Admin or Staff only
        """
        if self.action in ["list", "upcoming", "create"]:
            return [IsAuthenticated()]
        if self.action in ["retrieve", "cancel"]:
            return [IsAuthenticated(), IsAppointmentParticipant()]
        if self.action in ["update", "partial_update", "destroy"]:
            return [IsAdminOrStaff()]
        return [IsAuthenticated()]

    def get_queryset(self):
        """
        Queryset filtering — prevents URL manipulation.

        PATIENT → only their own appointments
        DOCTOR  → only their own appointments (APPROVED / VISITED)
        ADMIN / STAFF → all appointments
        """
        user = self.request.user
        queryset = Appointment.objects.none()

        if user.role == "PATIENT":
            queryset = Appointment.objects.filter(patient__user=user)
        elif user.role == "DOCTOR":
            # Doctor sees ONLY Approved or Visited appointments
            queryset = Appointment.objects.filter(
                doctor__user=user, status__in=["APPROVED", "VISITED"]
            )
        elif user.role in ["ADMIN", "STAFF"]:
            queryset = Appointment.objects.all()

        # Optimize query
        queryset = queryset.select_related("patient__user", "doctor__user")

        # Filter by patient_id if provided (for history)
        patient_id = self.request.query_params.get("patient_id")
        if patient_id:
            queryset = queryset.filter(patient_id=patient_id)

        return queryset

    # ------------------------------------------------------------------
    # Helper: validate a status transition
    # ------------------------------------------------------------------
    def _validate_transition(self, current_status, new_status):
        """
        Return True if transitioning from current_status → new_status
        is allowed by the appointment lifecycle.
        """
        allowed = Appointment.VALID_TRANSITIONS.get(current_status, set())
        return new_status in allowed

    # ------------------------------------------------------------------
    # CREATE  — with atomic + IntegrityError guard
    # ------------------------------------------------------------------
    def perform_create(self, serializer):
        """
        When creating a new appointment:
        - If user is PATIENT, automatically assign them as the patient
        - If user is ADMIN/STAFF, they can specify the patient
        - DOCTOR role is rejected (doctors receive appointments, not create)

        Uses transaction.atomic() so the serializer validation + INSERT
        happen inside one transaction.  If a concurrent request sneaks
        past the serializer check, the database UniqueConstraint raises
        IntegrityError which we convert to HTTP 409.
        """
        user = self.request.user

        if user.role == "DOCTOR":
            raise serializers.ValidationError(
                {"error": "Doctors cannot create appointments."}
            )

        try:
            with transaction.atomic():
                if user.role == "PATIENT":
                    # Ensure the patient exists for this user
                    try:
                        from patients.models import Patient

                        patient = Patient.objects.get(user=user)
                        appointment = serializer.save(patient=patient)

                        # Notify Admins of new appointment
                        admins = User.objects.filter(role="ADMIN")
                        for admin in admins:
                            Notification.objects.create(
                                user=admin,
                                title="New Appointment Request",
                                message=f"New appointment request from {user.full_name} for Dr. {appointment.doctor.user.full_name}",
                            )
                    except Patient.DoesNotExist:
                        raise serializers.ValidationError(
                            {"error": "Patient profile not found."}
                        )
                else:
                    appointment = serializer.save()

        except IntegrityError as e:
            if "unique_active_doctor_slot" in str(e):
                raise serializers.ValidationError(
                    {
                        "appointment_time": (
                            "This slot was just booked by another user. "
                            "Please choose a different time."
                        )
                    }
                )
            raise  # Re-raise unexpected IntegrityErrors

    # ------------------------------------------------------------------
    # UPDATE  — with atomic + select_for_update + transition validation
    # ------------------------------------------------------------------
    def perform_update(self, serializer):
        """
        Wrap every update in a transaction with a row-level lock to
        prevent concurrent status changes from creating inconsistencies.
        """
        try:
            with transaction.atomic():
                # Lock the row to prevent concurrent modifications
                instance = (
                    Appointment.objects.select_for_update()
                    .get(pk=self.get_object().pk)
                )
                old_status = instance.status
                new_status = serializer.validated_data.get("status", old_status)

                # Validate state transition if status is changing
                if new_status != old_status:
                    if not self._validate_transition(old_status, new_status):
                        raise serializers.ValidationError(
                            {
                                "status": (
                                    f"Cannot transition from {old_status} "
                                    f"to {new_status}."
                                )
                            }
                        )

                appointment = serializer.save()

                # Check for status changes and send notifications
                if old_status != appointment.status:
                    if appointment.status == "APPROVED":
                        # Notify Patient
                        Notification.objects.create(
                            user=appointment.patient.user,
                            title="Appointment Approved",
                            message=f"Your appointment with Dr. {appointment.doctor.user.full_name} has been approved",
                        )
                        # Notify Doctor
                        Notification.objects.create(
                            user=appointment.doctor.user,
                            title="New Appointment",
                            message=f"You have a new appointment with {appointment.patient.user.full_name} on {appointment.appointment_date}",
                        )
                    elif appointment.status == "REJECTED":
                        Notification.objects.create(
                            user=appointment.patient.user,
                            title="Appointment Rejected",
                            message=f"Your appointment with Dr. {appointment.doctor.user.full_name} has been rejected",
                        )
                    elif appointment.status == "VISITED":
                        Notification.objects.create(
                            user=appointment.patient.user,
                            title="Consultation Completed",
                            message=f"Your visit with Dr. {appointment.doctor.user.full_name} has been marked as completed",
                        )

        except IntegrityError as e:
            if "unique_active_doctor_slot" in str(e):
                raise serializers.ValidationError(
                    {
                        "appointment_time": (
                            "This slot was just booked by another user. "
                            "Please choose a different time."
                        )
                    }
                )
            raise

    # ------------------------------------------------------------------
    # APPROVE
    # ------------------------------------------------------------------
    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        """Approve an appointment - only ADMIN can approve"""
        user = request.user

        # Only ADMIN can approve appointments
        if user.role != "ADMIN":
            return Response(
                {"error": "Only administrators can approve appointments"},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            with transaction.atomic():
                # Lock the appointment row
                appointment = (
                    Appointment.objects.select_for_update()
                    .get(pk=self.get_object().pk)
                )

                # Validate lifecycle transition
                if not self._validate_transition(appointment.status, "APPROVED"):
                    return Response(
                        {
                            "error": (
                                f"Cannot approve appointment with status "
                                f"'{appointment.status}'."
                            )
                        },
                        status=status.HTTP_409_CONFLICT,
                    )

                appointment.status = "APPROVED"
                appointment.save()

                # Notify patient of approval
                Notification.objects.create(
                    user=appointment.patient.user,
                    title="Appointment Approved",
                    message=f"Your appointment with Dr. {appointment.doctor.user.full_name} has been approved",
                )

                # Notify Doctor
                Notification.objects.create(
                    user=appointment.doctor.user,
                    title="New Appointment Scheduled",
                    message=f"You have a new appointment with {appointment.patient.user.full_name} on {appointment.appointment_date} at {appointment.appointment_time}",
                )

        except IntegrityError as e:
            if "unique_active_doctor_slot" in str(e):
                return Response(
                    {
                        "error": (
                            "This slot was just booked by another user. "
                            "Please choose a different time."
                        )
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            raise

        return Response(
            {"status": "APPROVED", "message": "Appointment approved successfully"}
        )

    # ------------------------------------------------------------------
    # CANCEL
    # ------------------------------------------------------------------
    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        """
        Cancel an appointment.
        - Patients can cancel only their own *active* appointments
          (PENDING or APPROVED).
        - Admins can cancel any appointment.
        """
        user = request.user

        with transaction.atomic():
            # Lock the appointment row
            appointment = (
                Appointment.objects.select_for_update()
                .get(pk=self.get_object().pk)
            )

            # Validate lifecycle transition
            if not self._validate_transition(appointment.status, "CANCELLED"):
                return Response(
                    {
                        "error": (
                            f"Cannot cancel appointment with status "
                            f"'{appointment.status}'."
                        )
                    },
                    status=status.HTTP_409_CONFLICT,
                )

            can_cancel = False
            if user.role == "PATIENT" and appointment.patient.user == user:
                # Only allow cancelling active appointments
                if appointment.status in Appointment.ACTIVE_STATUSES:
                    can_cancel = True
            elif user.role == "ADMIN":
                can_cancel = True

            if not can_cancel:
                return Response(
                    {"error": "You do not have permission to cancel this appointment"},
                    status=status.HTTP_403_FORBIDDEN,
                )

            appointment.status = "CANCELLED"
            appointment.save()

            # Notify patient of cancellation (if not cancelled by patient)
            if appointment.patient.user != user:
                Notification.objects.create(
                    user=appointment.patient.user,
                    title="Appointment Cancelled",
                    message=f"Your appointment with Dr. {appointment.doctor.user.full_name} has been cancelled",
                )

        return Response(
            {"status": "CANCELLED", "message": "Appointment cancelled successfully"}
        )

    # ------------------------------------------------------------------
    # REJECT
    # ------------------------------------------------------------------
    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        """Reject/Decline an appointment - only ADMIN can reject"""
        user = request.user

        # Only ADMIN can reject appointments
        if user.role != "ADMIN":
            return Response(
                {"error": "Only administrators can reject appointments"},
                status=status.HTTP_403_FORBIDDEN,
            )

        with transaction.atomic():
            # Lock the appointment row
            appointment = (
                Appointment.objects.select_for_update()
                .get(pk=self.get_object().pk)
            )

            # Validate lifecycle transition
            if not self._validate_transition(appointment.status, "REJECTED"):
                return Response(
                    {
                        "error": (
                            f"Cannot reject appointment with status "
                            f"'{appointment.status}'."
                        )
                    },
                    status=status.HTTP_409_CONFLICT,
                )

            reason = request.data.get("reason", "No reason provided")
            appointment.status = "REJECTED"
            appointment.save()

            # Notify patient of rejection
            Notification.objects.create(
                user=appointment.patient.user,
                title="Appointment Rejected",
                message=f"Your appointment with Dr. {appointment.doctor.user.full_name} has been rejected. Reason: {reason}",
            )

        return Response({"status": "REJECTED", "message": "Appointment rejected"})

    # ------------------------------------------------------------------
    # UPCOMING
    # ------------------------------------------------------------------
    @action(detail=False, methods=["get"])
    def upcoming(self, request):
        """Get upcoming appointments"""
        today = timezone.now().date()
        appointments = (
            self.get_queryset()
            .filter(appointment_date__gte=today, status__in=["PENDING", "APPROVED"])
            .select_related("patient__user", "doctor__user")
        )
        serializer = self.get_serializer(appointments, many=True)
        return Response(serializer.data)
