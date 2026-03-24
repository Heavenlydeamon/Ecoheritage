"""
AI Lesson Generator Views
=======================
Django views for generating robust study materials (lessons) and related quizzes.

Features:
- Teacher lesson generation via prompt
- Creation of Class, Section, Topic, and StudyMaterial records automatically
- Chain-generation of a an AI Quiz attached to the lesson
"""

import logging
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.db import transaction

from mainapp.models import (
    StudyMaterial, Section, Topic, Class, UserProfile,
    AIGeneratedQuiz, AIGeneratedQuestion, AIGeneratedChoice, Institution
)
from mainapp.ai_lesson_generator import AILessonGenerator, LessonGenerationError
from mainapp.ai_quiz_generator import generate_quiz_from_text, QuizGenerationError, InputValidationError

logger = logging.getLogger(__name__)


@login_required
def teacher_ai_lesson_generator(request):
    """
    Renders the form for teachers to generate a new AI Lesson.
    """
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'teacher':
            return redirect('student_dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
        
    context = {
        'profile': profile,
    }
    
    return render(request, 'teacher/teacher_ai_lesson_generator.html', context)


@login_required
def teacher_generate_lesson_api(request):
    """
    POST endpoint to generate the lesson content ONLY (for preview).
    Required POST params: title, context_text, action
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid method'}, status=405)
        
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'teacher':
            return JsonResponse({'error': 'Permission denied'}, status=403)
    except UserProfile.DoesNotExist:
        return JsonResponse({'error': 'Profile not found'}, status=404)
        
    title = request.POST.get('title')
    context_text = request.POST.get('context_text', '')
    action = request.POST.get('action', 'generate_lesson')
    length = request.POST.get('length', 'medium')
    
    if not title:
        return JsonResponse({'error': 'Title is required.'}, status=400)
        
    generator = AILessonGenerator()
    try:
        if action == 'generate_summary':
            generated_content = generator.generate_summary_only(title, context_text)
        elif action == 'generate_key_terms':
            generated_content = generator.generate_key_terms_only(title, context_text)
        else:
            generated_content = generator.generate_lesson(title, context_text, length=length)
            
    except LessonGenerationError as e:
        return JsonResponse({'error': f'Generation failed: {str(e)}'}, status=500)
    except Exception as e:
        logger.error(f"Unexpected error during generation: {str(e)}")
        return JsonResponse({'error': 'An unexpected error occurred during generation.'}, status=500)
        
    return JsonResponse({
        'success': True,
        'generated_content': generated_content,
        'action': action
    })


@login_required
def teacher_publish_lesson_api(request):
    """
    POST endpoint to save the lesson and generate a quiz.
    Required POST params: title, content, class_name, section_name
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid method'}, status=405)
        
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'teacher':
            return JsonResponse({'error': 'Permission denied'}, status=403)
    except UserProfile.DoesNotExist:
        return JsonResponse({'error': 'Profile not found'}, status=404)
        
    title = request.POST.get('title')
    content = request.POST.get('content')
    class_name = request.POST.get('class_name')
    section_name = request.POST.get('section_name')
    
    if not all([title, content, class_name, section_name]):
        return JsonResponse({'error': 'All fields are required for publishing.'}, status=400)
        
    try:
        institution = profile.institution or Institution.objects.first()
        if not institution:
            institution = Institution.objects.create(name="Default Institution")
                
        class_obj, created_class = Class.objects.get_or_create(
            name=class_name,
            institution=institution,
            teacher=request.user,
            defaults={'subject': class_name}
        )
        
        if created_class:
            teacher_students = UserProfile.objects.filter(role='student', student_class__teacher=request.user).distinct()
            for student_profile in teacher_students:
                student_profile.student_class.add(class_obj)
        
        section, _ = Section.objects.get_or_create(
            name=section_name,
            class_obj=class_obj,
            defaults={'description': f'Automatically created section for {class_name}'}
        )
        
        with transaction.atomic():
            max_order = Topic.objects.filter(section=section).count()
            new_topic = Topic.objects.create(
                section=section,
                name=title,
                description=f"AI Generated lesson about {title}",
                order=max_order
            )
            
            study_material = StudyMaterial.objects.create(
                topic=new_topic,
                title=title,
                content_text=content,
                difficulty='beginner',
                estimated_time='medium'
            )
            
        # Generate Quiz from the final edited content (OUTSIDE atomic to avoid DB locking)
        generated_questions = []
        quiz_id = None
        try:
            # Only generate quiz if content is long enough
            if content and len(content) > 100:
                generated_questions = generate_quiz_from_text(content, num_questions=5)
        except Exception as e:
            logger.warning(f"Could not generate quiz for {title}: {e}")

        if generated_questions:
            try:
                with transaction.atomic():
                    quiz = AIGeneratedQuiz.objects.create(
                        title=f"AI Quiz - {title}",
                        description=f"Generated from AI Lesson: {title}",
                        study_material=study_material,
                        content_type='class',
                        section=section,
                        topic=new_topic,
                        class_obj=class_obj,
                        status='draft',
                        generated_by=request.user
                    )
                    
                    quiz_id = quiz.id
                    
                    for i, q_data in enumerate(generated_questions):
                        question = AIGeneratedQuestion.objects.create(
                            quiz=quiz,
                            question_text=q_data['question_text'],
                            difficulty=q_data.get('difficulty', 'medium'),
                            order=i + 1
                        )
                        
                        options = q_data.get('options', [])
                        correct_answer = q_data.get('correct_answer', '')
                        
                        for j, option_text in enumerate(options):
                            is_correct = (
                                option_text == correct_answer or 
                                (correct_answer and correct_answer.strip().lower() == option_text.strip().lower()) or
                                (correct_answer and len(option_text) > 10 and correct_answer.startswith(option_text[:20]))
                            )
                            AIGeneratedChoice.objects.create(
                                question=question,
                                choice_text=option_text,
                                is_correct=is_correct,
                                order=j + 1
                            )
            except Exception as e:
                logger.error(f"Error saving generated quiz: {e}")

        return JsonResponse({
            'success': True,
            'message': 'Lesson published successfully!',
            'topic_id': new_topic.id,
            'study_material_id': study_material.id,
            'quiz_id': quiz_id,
            'topic_url': f"/topic/{new_topic.id}/study/",
            'quiz_url': f"/teacher/ai-quiz/preview/{quiz_id}/" if quiz_id else None
        })
        
    except Exception as e:
        logger.error(f"Error publishing AI generated lesson: {str(e)}")
        return JsonResponse({'error': f'Error publishing lesson: {str(e)}'}, status=500)

@login_required
def teacher_suggest_topics_api(request):
    """
    POST endpoint to suggest topics for a subject.
    Required POST params: subject_name
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid method'}, status=405)
        
    subject_name = request.POST.get('subject_name')
    if not subject_name:
        return JsonResponse({'error': 'Subject name is required.'}, status=400)
        
    generator = AILessonGenerator()
    try:
        topics = generator.suggest_topics(subject_name)
        return JsonResponse({
            'success': True,
            'topics': topics
        })
    except Exception as e:
        logger.error(f"Error suggesting topics: {str(e)}")
        return JsonResponse({'error': 'Failed to suggest topics.'}, status=500)
