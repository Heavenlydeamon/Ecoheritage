"""
AI Quiz Generator Views
=======================
Django views for AI-powered quiz generation with approval workflows.

Features:
- Teacher quiz generation from study materials
- Manual quiz creation option for teachers
- Teacher approval for class-specific content
- Admin approval for general content
"""

import logging

from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.utils import timezone
from django.db import transaction

from mainapp.models import (
    StudyMaterial, Section, Topic, Class, Question, Choice,
    AIGeneratedQuiz, AIGeneratedQuestion, AIGeneratedChoice,
    UserProfile
)
from mainapp.ai_quiz_generator import (
    generate_quiz_from_text,
    get_quiz_generator,
    QuizGenerationError,
    InputValidationError,
    ModelLoadError,
    MIN_INPUT_LENGTH,
    MAX_INPUT_LENGTH,
    extract_text_from_pdf,
    PDF_SUPPORT,
)

logger = logging.getLogger(__name__)


# ============================================
# TEACHER AI QUIZ GENERATOR VIEWS

@login_required
def teacher_ai_quiz_generator(request):
    """
    Main view for teachers to generate AI quizzes for their class.
    
    Teachers can:
    - Select study material from their class to generate quiz from
    - Preview and edit generated questions
    - Submit for approval or save as draft
    """
    try:
        profile = UserProfile.objects.get(user=request.user)
        # FIX Issue 19: Only teachers should access this, admins use admin panel
        if profile.role != 'teacher':
            if profile.role == 'admin':
                return redirect('admin_dashboard')
            return redirect('student_dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    # Get class_id from query parameter
    class_id = request.GET.get('class_id')
    
    # Get teacher's classes
    teacher_classes = Class.objects.filter(teacher=request.user)
    
    # If class_id provided, validate it belongs to this teacher
    selected_class = None
    class_study_materials = []
    
    if class_id:
        try:
            selected_class = Class.objects.get(id=class_id, teacher=request.user)
            # Get study materials for this class's sections/topics
            class_sections = Section.objects.filter(class_obj=selected_class)
            class_topics = Topic.objects.filter(section__in=class_sections)
            class_study_materials = StudyMaterial.objects.filter(
                topic__in=class_topics
            ).select_related('topic', 'topic__section').distinct()
        except Class.DoesNotExist:
            messages.error(request, 'Class not found or you do not have permission.')
            return redirect('teacher_manage_classes')
    else:
        # Show all class study materials from all teacher's classes
        class_sections = Section.objects.filter(class_obj__teacher=request.user)
        class_topics = Topic.objects.filter(section__in=class_sections)
        class_study_materials = StudyMaterial.objects.filter(
            topic__in=class_topics
        ).select_related('topic', 'topic__section').distinct()
    
    context = {
        'teacher_classes': teacher_classes,
        'selected_class': selected_class,
        'class_study_materials': class_study_materials,
        'profile': profile,
    }
    
    return render(request, 'teacher/teacher_ai_quiz_generator.html', context)


@login_required
def teacher_generate_quiz(request):
    """
    Generate AI quiz from study material for class-specific content.
    
    POST parameters:
    - study_material_id: ID of the study material
    - num_questions: Number of questions to generate (default: 10)
    - content_type: Always 'class' for class-specific content
    - class_id: Required for class content
    """
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'teacher' and profile.role != 'admin':
            return JsonResponse({'error': 'Permission denied'}, status=403)
    except UserProfile.DoesNotExist:
        return JsonResponse({'error': 'Profile not found'}, status=404)
    
    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid method'}, status=405)
    
    try:
        # Get parameters
        study_material_id = request.POST.get('study_material_id')
        num_questions = int(request.POST.get('num_questions', 10))
        content_type = 'class'  # Always class-specific
        class_id = request.POST.get('class_id')
        
        # Validate study material
        if not study_material_id:
            return JsonResponse({'error': 'Study material is required'}, status=400)
        
        try:
            study_material = StudyMaterial.objects.select_related('topic', 'topic__section').get(id=study_material_id)
        except StudyMaterial.DoesNotExist:
            return JsonResponse({'error': 'Study material not found'}, status=404)
        
        # Get section and topic from study material
        section = study_material.topic.section if study_material.topic else None
        topic = study_material.topic
        
        if not section:
            return JsonResponse({'error': 'Study material must be associated with a section'}, status=400)
        
        # Validate class
        if not class_id:
            return JsonResponse({'error': 'Class is required for class-specific content'}, status=400)
        
        try:
            class_obj = Class.objects.get(id=class_id)
            # Verify teacher owns this class
            if profile.role != 'admin' and class_obj.teacher != request.user:
                return JsonResponse({'error': 'Permission denied for this class'}, status=403)
        except Class.DoesNotExist:
            return JsonResponse({'error': 'Class not found'}, status=404)
        
        # Check that study material belongs to this class
        if topic and topic.section.class_obj_id != class_obj.id:
            return JsonResponse({'error': 'Study material does not belong to the selected class'}, status=400)
        
        # Check input text length
        text = study_material.content_text
        if len(text) < MIN_INPUT_LENGTH:
            return JsonResponse({
                'error': f'Study material too short. Minimum {MIN_INPUT_LENGTH} characters required. Current: {len(text)} characters. Please add more content to the study material before generating a quiz.'
            }, status=400)
        
        # ISSUE 12 FIX: Check if quiz already exists for this topic/section combination
        if topic:
            existing_quiz = AIGeneratedQuiz.objects.filter(
                topic=topic,
                status__in=['approved', 'pending']
            ).first()
            if existing_quiz:
                return JsonResponse({
                    'error': f'Quiz already exists for this topic. Please delete the existing quiz first or use a different topic.'
                }, status=400)
        else:
            # Check by section and study material if no topic
            existing_quiz = AIGeneratedQuiz.objects.filter(
                section=section,
                study_material=study_material,
                status__in=['approved', 'pending']
            ).first()
            if existing_quiz:
                return JsonResponse({
                    'error': f'Quiz already exists for this study material. Please delete the existing quiz first.'
                }, status=400)
        
        # Generate quiz using AI
        try:
            generated_questions = generate_quiz_from_text(text, num_questions)
        except InputValidationError as e:
            return JsonResponse({'error': str(e)}, status=400)
        except QuizGenerationError as e:
            return JsonResponse({'error': f'Generation failed: {str(e)}'}, status=500)
        except Exception as e:
            return JsonResponse({'error': f'Unexpected error: {str(e)}'}, status=500)
        
        if not generated_questions:
            return JsonResponse({'error': 'No questions could be generated from the material'}, status=500)
        
        # Create quiz in database
        with transaction.atomic():
            # Create the quiz
            quiz = AIGeneratedQuiz.objects.create(
                title=f"AI Quiz - {study_material.title}",
                description=f"Generated from: {study_material.title}",
                study_material=study_material,
                content_type=content_type,
                section=section,
                topic=topic,
                class_obj=class_obj,
                status='draft',
                generated_by=request.user
            )
            
            # Create questions
            for i, q_data in enumerate(generated_questions):
                question = AIGeneratedQuestion.objects.create(
                    quiz=quiz,
                    question_text=q_data['question_text'],
                    difficulty=q_data.get('difficulty', 'medium'),
                    order=i + 1
                )
                
                # Create choices
                options = q_data.get('options', [])
                correct_answer = q_data.get('correct_answer', '')
                
                for j, option_text in enumerate(options):
                    # ISSUE 2 FIX: Use startswith() for flexible matching
                    is_correct = (
                        option_text == correct_answer or 
                        correct_answer.startswith(option_text[:20]) if correct_answer else False
                    )
                    AIGeneratedChoice.objects.create(
                        question=question,
                        choice_text=option_text,
                        is_correct=is_correct,
                        order=j + 1
                    )
        
        return JsonResponse({
            'success': True,
            'quiz_id': quiz.id,
            'message': f'Successfully generated {len(generated_questions)} questions',
            'questions': [
                {
                    'id': q.id,
                    'text': q.question_text,
                    'difficulty': q.difficulty,
                    'options': [
                        {'id': c.id, 'text': c.choice_text, 'is_correct': c.is_correct}
                        for c in q.choices.all()
                    ]
                }
                for q in quiz.questions.all()
            ]
        })
        
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@login_required
def teacher_generate_quiz_from_material(request, material_id):
    """
    Generate AI quiz directly from a study material ID.
    This is a simplified version that uses the material's content directly.
    
    URL: /teacher/ai-quiz/generate-from-material/<int:material_id>/
    
    Returns JSON response with success/error or redirects to quiz preview.
    """
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'teacher' and profile.role != 'admin':
            return JsonResponse({'error': 'Permission denied'}, status=403)
    except UserProfile.DoesNotExist:
        return JsonResponse({'error': 'Profile not found'}, status=404)
    
    # Get the study material
    try:
        study_material = StudyMaterial.objects.select_related(
            'topic', 'topic__section', 'topic__section__class_obj'
        ).get(id=material_id)
    except StudyMaterial.DoesNotExist:
        messages.error(request, 'Study material not found.')
        return redirect('teacher_manage_classes')
    
    # Get section and topic from study material
    topic = study_material.topic
    if not topic:
        messages.error(request, 'Study material must be associated with a topic.')
        return redirect('teacher_manage_classes')
    
    section = topic.section
    if not section:
        messages.error(request, 'Topic must be associated with a section.')
        return redirect('teacher_manage_classes')
    
    # Get the class this material belongs to
    class_obj = section.class_obj
    if not class_obj:
        messages.error(request, 'This material is not linked to a class.')
        return redirect('teacher_manage_classes')
    
    # Verify teacher owns this class
    if profile.role != 'admin' and class_obj.teacher != request.user:
        messages.error(request, 'You do not have permission to generate quiz for this material.')
        return redirect('teacher_manage_classes')
    
    # Check if quiz already exists for this material
    existing_quiz = AIGeneratedQuiz.objects.filter(
        study_material=study_material,
        status__in=['approved', 'pending']
    ).first()
    
    if existing_quiz:
        messages.warning(request, f'Quiz already exists for this material. Status: {existing_quiz.status}')
        return redirect('teacher_preview_quiz', quiz_id=existing_quiz.id)
    
    # Get content text - try PDF extraction if available
    text = study_material.content_text
    
    # Check if there's a PDF file to extract text from
    if study_material.video and hasattr(study_material.video, 'url'):
        # Check if it's actually a PDF by file extension
        if study_material.video.url.lower().endswith('.pdf'):
            try:
                if PDF_SUPPORT:
                    # Extract text from PDF
                    # Note: This would require the actual file path
                    pass
            except Exception as e:
                logger.warning(f"Could not extract PDF text: {e}")
    
    # Check input text length
    if not text or len(text) < MIN_INPUT_LENGTH:
        messages.error(request, f'Study material too short. Minimum {MIN_INPUT_LENGTH} characters required. Current: {len(text) if text else 0} characters. Please add more content to the study material before generating a quiz.')
        return redirect('teacher_manage_classes')
    
    # Generate quiz using AI
    num_questions = 10
    content_type = 'class'
    
    try:
        generated_questions = generate_quiz_from_text(text, num_questions)
    except InputValidationError as e:
        messages.error(request, str(e))
        return redirect('teacher_manage_classes')
    except QuizGenerationError as e:
        messages.error(request, f'Generation failed: {str(e)}')
        return redirect('teacher_manage_classes')
    except Exception as e:
        messages.error(request, f'Unexpected error: {str(e)}')
        return redirect('teacher_manage_classes')
    
    if not generated_questions:
        messages.error(request, 'No questions could be generated from the material.')
        return redirect('teacher_manage_classes')
    
    # Create quiz in database
    try:
        with transaction.atomic():
            # Create the quiz
            quiz = AIGeneratedQuiz.objects.create(
                title=f"AI Quiz - {study_material.title}",
                description=f"Generated from: {study_material.title}",
                study_material=study_material,
                content_type=content_type,
                section=section,
                topic=topic,
                class_obj=class_obj,
                status='draft',
                generated_by=request.user
            )
            
            # Create questions
            for i, q_data in enumerate(generated_questions):
                question = AIGeneratedQuestion.objects.create(
                    quiz=quiz,
                    question_text=q_data['question_text'],
                    difficulty=q_data.get('difficulty', 'medium'),
                    order=i + 1
                )
                
                # Create choices
                options = q_data.get('options', [])
                correct_answer = q_data.get('correct_answer', '')
                
                for j, option_text in enumerate(options):
                    is_correct = (
                        option_text == correct_answer or 
                        correct_answer.startswith(option_text[:20]) if correct_answer else False
                    )
                    AIGeneratedChoice.objects.create(
                        question=question,
                        choice_text=option_text,
                        is_correct=is_correct,
                        order=j + 1
                    )
        
        messages.success(request, f'Successfully generated {len(generated_questions)} questions! Review and submit for approval.')
        return redirect('teacher_preview_quiz', quiz_id=quiz.id)
        
    except Exception as e:
        messages.error(request, f'Error saving quiz: {str(e)}')
        return redirect('teacher_manage_classes')


@login_required
def teacher_preview_quiz(request, quiz_id):
    """
    Preview and edit an AI-generated quiz before submission.
    """
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'teacher' and profile.role != 'admin':
            return redirect('student_dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    quiz = get_object_or_404(AIGeneratedQuiz, id=quiz_id)
    
    # Check permission
    if profile.role != 'admin' and quiz.generated_by != request.user:
        messages.error(request, 'You do not have permission to view this quiz.')
        return redirect('teacher_dashboard')
    
    context = {
        'quiz': quiz,
        'profile': profile,
    }
    
    return render(request, 'teacher/teacher_preview_quiz.html', context)


@login_required
def teacher_save_quiz(request, quiz_id):
    """
    Save quiz as draft or submit for approval.
    """
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'teacher' and profile.role != 'admin':
            return JsonResponse({'error': 'Permission denied'}, status=403)
    except UserProfile.DoesNotExist:
        return JsonResponse({'error': 'Profile not found'}, status=404)
    
    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid method'}, status=405)
    
    quiz = get_object_or_404(AIGeneratedQuiz, id=quiz_id)
    
    # Check permission
    if profile.role != 'admin' and quiz.generated_by != request.user:
        return JsonResponse({'error': 'Permission denied'}, status=403)
    
    action = request.POST.get('action', 'draft')
    if action == 'submit':
        # Auto-approve for teachers submitting class-specific content
        if quiz.content_type == 'class':
            quiz.status = 'approved'
            quiz.approved_by = request.user
            quiz.reviewed_at = timezone.now()
            # Also auto-verify all questions in the quiz
            quiz.questions.all().update(is_verified=True)
            messages.success(request, 'Quiz published successfully! It is now available to your students.')
        else:
            quiz.status = 'pending'
            quiz.submitted_at = timezone.now()
            messages.success(request, 'Quiz submitted for approval!')
    else:
        quiz.status = 'draft'
        messages.success(request, 'Quiz saved as draft!')
    
    quiz.save()
    
    return JsonResponse({'success': True, 'status': quiz.status})


@login_required
def teacher_update_question(request, question_id):
    """
    Update a question in an AI-generated quiz.
    """
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'teacher' and profile.role != 'admin':
            return JsonResponse({'error': 'Permission denied'}, status=403)
    except UserProfile.DoesNotExist:
        return JsonResponse({'error': 'Profile not found'}, status=404)
    
    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid method'}, status=405)
    
    question = get_object_or_404(AIGeneratedQuestion, id=question_id)
    
    # Check permission
    if profile.role != 'admin' and question.quiz.generated_by != request.user:
        return JsonResponse({'error': 'Permission denied'}, status=403)
    
    # Update question text
    question_text = request.POST.get('question_text')
    if question_text:
        question.question_text = question_text
    
    # Update difficulty
    difficulty = request.POST.get('difficulty')
    if difficulty in ['easy', 'medium', 'hard']:
        question.difficulty = difficulty
    
    question.save()
    
    # Update choices if provided
    choices_data = request.POST.get('choices')
    if choices_data:
        # Parse choices (expecting JSON format)
        import json
        try:
            choices_list = json.loads(choices_data)
            for i, choice_data in enumerate(choices_list):
                choice_id = choice_data.get('id')
                choice_text = choice_data.get('text')
                is_correct = choice_data.get('is_correct', False)
                
                if choice_id:
                    try:
                        choice = AIGeneratedChoice.objects.get(id=choice_id, question=question)
                        if choice_text:
                            choice.choice_text = choice_text
                        choice.is_correct = is_correct
                        choice.save()
                    except AIGeneratedChoice.DoesNotExist:
                        pass
        except json.JSONDecodeError:
            pass
    
    return JsonResponse({'success': True})


@login_required
def teacher_delete_quiz(request, quiz_id):
    """
    Delete an AI-generated quiz (only if draft).
    """
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'teacher' and profile.role != 'admin':
            return JsonResponse({'error': 'Permission denied'}, status=403)
    except UserProfile.DoesNotExist:
        return JsonResponse({'error': 'Profile not found'}, status=404)
    
    quiz = get_object_or_404(AIGeneratedQuiz, id=quiz_id)
    
    # Check permission and status
    if profile.role != 'admin' and quiz.generated_by != request.user:
        return JsonResponse({'error': 'Permission denied'}, status=403)
    
    if quiz.status != 'draft':
        return JsonResponse({'error': 'Can only delete draft quizzes'}, status=400)
    
    quiz.delete()
    messages.success(request, 'Quiz deleted successfully!')
    
    return JsonResponse({'success': True})


@login_required
def teacher_my_quizzes(request):
    """
    View all AI-generated quizzes by the teacher.
    """
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'teacher' and profile.role != 'admin':
            return redirect('student_dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    # Get quizzes based on role
    if profile.role == 'admin':
        quizzes = AIGeneratedQuiz.objects.all()
    else:
        quizzes = AIGeneratedQuiz.objects.filter(generated_by=request.user)
    
    # Filter by status
    status_filter = request.GET.get('status')
    if status_filter:
        quizzes = quizzes.filter(status=status_filter)
    
    quizzes = quizzes.select_related('section', 'topic', 'study_material', 'generated_by')
    
    context = {
        'quizzes': quizzes,
        'status_filter': status_filter,
        'profile': profile,
    }
    
    return render(request, 'teacher/teacher_my_quizzes.html', context)


# ============================================
# TEACHER MANUAL QUIZ CREATION VIEWS
# ============================================

@login_required
def teacher_manual_quiz(request):
    """
    Manual quiz creation (alternative to AI generation).
    """
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'teacher' and profile.role != 'admin':
            return redirect('student_dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    sections = Section.objects.all()
    topics = Topic.objects.all()
    
    if profile.role == 'admin':
        teacher_classes = Class.objects.all()
    else:
        teacher_classes = Class.objects.filter(teacher=request.user)
    
    context = {
        'sections': sections,
        'topics': topics,
        'teacher_classes': teacher_classes,
        'profile': profile,
    }
    
    return render(request, 'teacher/teacher_manual_quiz.html', context)


@login_required
def teacher_create_manual_quiz(request):
    """
    Create a manual quiz with questions.
    """
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'teacher' and profile.role != 'admin':
            return JsonResponse({'error': 'Permission denied'}, status=403)
    except UserProfile.DoesNotExist:
        return JsonResponse({'error': 'Profile not found'}, status=404)
    
    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid method'}, status=405)
    
    try:
        # Get basic quiz info
        title = request.POST.get('title')
        section_id = request.POST.get('section_id')
        topic_id = request.POST.get('topic_id', None)
        content_type = request.POST.get('content_type', 'general')
        class_id = request.POST.get('class_id', None)
        
        if not title or not section_id:
            return JsonResponse({'error': 'Title and section are required'}, status=400)
        
        section = Section.objects.get(id=section_id)
        topic = Topic.objects.get(id=topic_id) if topic_id else None
        
        class_obj = None
        if content_type == 'class' and class_id:
            class_obj = Class.objects.get(id=class_id)
            if profile.role != 'admin' and class_obj.teacher != request.user:
                return JsonResponse({'error': 'Permission denied for this class'}, status=403)
        
        # Create quiz
        quiz = AIGeneratedQuiz.objects.create(
            title=title,
            description=request.POST.get('description', ''),
            study_material_id=request.POST.get('study_material_id'),
            content_type=content_type,
            section=section,
            topic=topic,
            class_obj=class_obj,
            status='draft',
            generated_by=request.user
        )
        
        # Process questions
        num_questions = int(request.POST.get('num_questions', 0))
        
        for q_num in range(1, num_questions + 1):
            question_text = request.POST.get(f'question_text_{q_num}')
            if not question_text:
                continue
            
            difficulty = request.POST.get(f'difficulty_{q_num}', 'medium')
            
            question = AIGeneratedQuestion.objects.create(
                quiz=quiz,
                question_text=question_text,
                difficulty=difficulty,
                order=q_num
            )
            
            # Get choices
            correct_choice = int(request.POST.get(f'q{q_num}_correct', 1))
            
            for choice_num in range(1, 5):
                choice_text = request.POST.get(f'q{q_num}_choice{choice_num}')
                if choice_text:
                    AIGeneratedChoice.objects.create(
                        question=question,
                        choice_text=choice_text,
                        is_correct=(choice_num == correct_choice),
                        order=choice_num
                    )
        
        return JsonResponse({
            'success': True,
            'quiz_id': quiz.id,
            'message': 'Quiz created successfully!'
        })
        
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# ============================================
# APPROVAL WORKFLOW VIEWS - Teacher Only
# ============================================

@login_required
def teacher_quiz_approval(request):
    """
    Teacher approves/rejects quizzes for their class content.
    """
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'teacher' and profile.role != 'admin':
            return redirect('student_dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    # Get pending quizzes for teacher's classes
    if profile.role == 'admin':
        pending_quizzes = AIGeneratedQuiz.objects.filter(
            status='pending',
            content_type='class'
        ).select_related('section', 'topic', 'generated_by', 'class_obj')
    else:
        pending_quizzes = AIGeneratedQuiz.objects.filter(
            status='pending',
            content_type='class',
            class_obj__teacher=request.user
        ).select_related('section', 'topic', 'generated_by', 'class_obj')
    
    context = {
        'pending_quizzes': pending_quizzes,
        'profile': profile,
    }
    
    return render(request, 'teacher/teacher_quiz_approval.html', context)


@login_required
def process_teacher_quiz_approval(request, quiz_id):
    """
    Process teacher approval/rejection for class content quiz.
    """
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'teacher' and profile.role != 'admin':
            return JsonResponse({'error': 'Permission denied'}, status=403)
    except UserProfile.DoesNotExist:
        return JsonResponse({'error': 'Profile not found'}, status=404)
    
    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid method'}, status=405)
    
    quiz = get_object_or_404(AIGeneratedQuiz, id=quiz_id)
    
    # Check permission
    if profile.role != 'admin':
        if quiz.class_obj and quiz.class_obj.teacher != request.user:
            return JsonResponse({'error': 'Permission denied'}, status=403)
    
    action = request.POST.get('action')
    
    if action == 'approve':
        quiz.status = 'approved'
        quiz.approved_by = request.user
        quiz.reviewed_at = timezone.now()
        messages.success(request, f'Quiz "{quiz.title}" approved!')
    elif action == 'reject':
        quiz.status = 'rejected'
        quiz.approved_by = request.user
        quiz.reviewed_at = timezone.now()
        quiz.rejection_reason = request.POST.get('reason', '')
        messages.success(request, f'Quiz "{quiz.title}" rejected.')
    else:
        return JsonResponse({'error': 'Invalid action'}, status=400)
    
    quiz.save()
    
    return redirect('teacher_quiz_approval')


# ============================================
# CONVERT AI QUIZ TO REGULAR QUESTIONS
# ============================================

@login_required
def convert_quiz_to_questions(request, quiz_id):
    """
    Convert an approved AI quiz to regular questions in the system.
    """
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'teacher' and profile.role != 'admin':
            return JsonResponse({'error': 'Permission denied'}, status=403)
    except UserProfile.DoesNotExist:
        return JsonResponse({'error': 'Profile not found'}, status=404)
    
    quiz = get_object_or_404(AIGeneratedQuiz, id=quiz_id)
    
    # Check permission
    if profile.role != 'admin' and quiz.generated_by != request.user:
        return JsonResponse({'error': 'Permission denied'}, status=403)
    
    # Only approved quizzes can be converted
    if quiz.status != 'approved':
        return JsonResponse({'error': 'Only approved quizzes can be converted'}, status=400)
    
    try:
        with transaction.atomic():
            # Convert each question
            questions_created = 0
            
            for ai_question in quiz.questions.all():
                # Create main Question
                question = Question.objects.create(
                    section=quiz.section,
                    topic=quiz.topic,
                    question_text=ai_question.question_text,
                    difficulty=ai_question.difficulty
                )
                
                # Create choices
                for ai_choice in ai_question.choices.all():
                    Choice.objects.create(
                        question=question,
                        choice_text=ai_choice.choice_text,
                        is_correct=ai_choice.is_correct
                    )
                
                questions_created += 1
            
            # Update usage count
            quiz.used_count += 1
            quiz.save()
        
        return JsonResponse({
            'success': True,
            'message': f'Successfully converted {questions_created} questions!'
        })
        
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# ============================================
# AJAX HELPERS
# ============================================

@login_required
def get_study_materials_ajax(request):
    """AJAX: Get study materials for a topic or section"""
    topic_id = request.GET.get('topic_id')
    section_id = request.GET.get('section_id')
    
    study_materials = []
    
    if topic_id:
        materials = StudyMaterial.objects.filter(topic_id=topic_id)
    elif section_id:
        materials = StudyMaterial.objects.filter(topic__section_id=section_id)
    else:
        materials = StudyMaterial.objects.all()
    
    data = [
        {'id': m.id, 'title': m.title, 'content_length': len(m.content_text)}
        for m in materials[:20]
    ]
    
    return JsonResponse(data, safe=False)


@login_required
def get_topics_ajax(request):
    """AJAX: Get topics for a section"""
    section_id = request.GET.get('section_id')
    
    if not section_id:
        return JsonResponse([], safe=False)
    
    topics = Topic.objects.filter(section_id=section_id).order_by('order')
    data = [{'id': t.id, 'name': t.name} for t in topics]
    
    return JsonResponse(data, safe=False)


@login_required
def check_model_status(request):
    """AJAX: Check if AI model is loaded and return device info"""
    from mainapp.ai_quiz_generator import AIQuizGenerator
    from mainapp.ai_engine import AIEngine
    import torch
    import requests
    
    # Quizzes use Gemma via Ollama - check if Ollama is responsive
    gemma_ready = False
    try:
        response = requests.get("http://localhost:11434/api/tags", timeout=1)
        if response.status_code == 200:
            gemma_ready = True
    except:
        gemma_ready = False

    return JsonResponse({
        'loaded': AIQuizGenerator.is_model_loaded() or gemma_ready,
        'gemma_ready': gemma_ready,
        'device': 'cuda' if torch.cuda.is_available() else 'cpu',
        'model_name': 'Gemma:2b (Ollama)' if gemma_ready else AIEngine._model_name
    })


@login_required
def preload_model(request):
    """AJAX: Proactively load the AI model"""
    from mainapp.ai_engine import AIEngine
    try:
        AIEngine.load_model()
        return JsonResponse({'success': True, 'message': 'Model loaded successfully'})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

