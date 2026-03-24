from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.forms import ModelForm, modelformset_factory
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.db import models, transaction
from mainapp.models import Section, Topic, StudyMaterial, ConceptTag, MaterialView, MaterialAttempt, Question, Choice, QuizAttempt, UserProfile, StudentMarks, TopicProgress, Institution, Class, ChallengeAttempt, ChallengeQuestion, ClassJoinRequest, Enrollment, AIGeneratedQuiz, AIGeneratedQuestion, AIGeneratedChoice
from mainapp.views.ai_recommendations import get_personalized_dashboard_data, get_weak_concepts, get_recommended_materials
from mainapp.ai_quiz_generator import generate_quiz_from_text, QuizGenerationError, InputValidationError, MIN_INPUT_LENGTH
from mainapp.utils.gamification import get_level_title, update_streak, get_spirit_guide_message, award_random_collectible, check_for_artifact_shard, calculate_weekly_leaderboard, get_unread_league_notifications


# ============================================
# HELPER FUNCTIONS
# ============================================

# XP Reward System Constants
XP_REWARD_STUDY_TOPIC = 10  # XP for studying a topic (first time viewing study materials)
XP_REWARD_QUIZ_PASS = 20    # XP for passing a quiz (>=70%)
XP_REWARD_MODULE_COMPLETE = 50  # XP bonus when all topics in a section are completed


def award_xp(user, xp_amount, reason, profile=None):
    """
    Award XP to a user and handle level ups.
    
    Args:
        user: User object
        xp_amount: Amount of XP to award
        reason: String describing why XP was awarded (for logging)
        profile: UserProfile object (optional, will fetch if not provided)
    
    Returns:
        dict with 'xp_awarded', 'new_level', 'leveled_up' keys
    """
    if xp_amount <= 0:
        return {'xp_awarded': 0, 'new_level': 1, 'leveled_up': False}
    
    if profile is None:
        try:
            profile = UserProfile.objects.get(user=user)
        except UserProfile.DoesNotExist:
            return {'xp_awarded': 0, 'new_level': 1, 'leveled_up': False}
    
    # Only award XP to students
    if profile.role != 'student':
        return {'xp_awarded': 0, 'new_level': profile.level, 'leveled_up': False}
    
    # Calculate current level before XP
    old_level = profile.level
    
    # Add XP
    profile.xp += xp_amount
    profile.total_xp_earned += xp_amount
    
    # Check for level up (every 100 XP)
    # Level 1: 0-99, Level 2: 100-199, etc.
    new_level = (profile.xp // 100) + 1
    
    collected_item = None
    shard_obtained = None
    
    if new_level > old_level:
        profile.level = new_level
        # Award collectible on level up
        collected_item = award_random_collectible(profile)
        
    # Occasionally award artifact shards
    shard_obtained = check_for_artifact_shard(profile)
    
    # Update League assignment & weekly points
    from mainapp.models import League, UserLeague, LeagueNotification
    suitable_league = League.objects.filter(min_xp__lte=profile.total_xp_earned, max_xp__gte=profile.total_xp_earned).first()
    if suitable_league:
        ul, created = UserLeague.objects.get_or_create(profile=profile)
        if ul.league != suitable_league:
            # Create notification for league change
            if ul.league and not created:
                ntype = 'promotion' if suitable_league.min_xp > ul.league.min_xp else 'relegation'
                LeagueNotification.objects.create(
                    profile=profile,
                    notification_type=ntype,
                    old_league=ul.league,
                    new_league=suitable_league,
                    message=f"{'🎉 Promoted' if ntype == 'promotion' else '📉 Moved'} to {suitable_league.name}!"
                )
            elif created:
                LeagueNotification.objects.create(
                    profile=profile,
                    notification_type='new_league',
                    new_league=suitable_league,
                    message=f"🛡️ Welcome to {suitable_league.name}!"
                )
            ul.league = suitable_league
            ul.save()
        # Increment weekly points
        ul.points_this_week += xp_amount
        ul.save()
    
    profile.save()
    
    leveled_up = profile.level > old_level
    
    return {
        'xp_awarded': xp_amount,
        'new_level': profile.level,
        'level_title': get_level_title(profile.level),
        'leveled_up': leveled_up,
        'total_xp': profile.xp,
        'reason': reason,
        'collectible_unlocked': collected_item.name if collected_item else None,
        'shard_unlocked': shard_obtained.name if shard_obtained else None
    }


def check_module_completion(user, section):
    """
    Check if all topics in a section are completed.
    Award bonus XP if module is completed for the first time.
    
    Args:
        user: User object
        section: Section object
    
    Returns:
        dict with 'completed', 'xp_awarded' keys
    """
    from mainapp.models import Topic, TopicProgress
    
    # Get all topics in this section
    topics = Topic.objects.filter(section=section, is_general=True)
    
    if not topics.exists():
        return {'completed': False, 'xp_awarded': 0}
    
    # Check if all topics are completed
    completed_count = TopicProgress.objects.filter(
        user=user,
        topic__in=topics,
        status='completed'
    ).count()
    
    if completed_count == topics.count() and completed_count > 0:
        # All topics completed - check if we've already awarded module completion XP
        # Use a session-based approach or store in profile to track first-time completion
        # For now, we'll just award XP if they haven't reached the threshold
        profile = UserProfile.objects.get(user=user)
        
        # Award module completion bonus
        result = award_xp(user, XP_REWARD_MODULE_COMPLETE, 'Module completed', profile)
        return {
            'completed': True,
            'xp_awarded': result['xp_awarded'],
            'leveled_up': result.get('leveled_up', False)
        }
    
    return {'completed': False, 'xp_awarded': 0}


def award_study_xp(user, topic, profile=None):
    """
    Award XP for studying a topic.
    Only awards XP the first time a topic is studied.
    
    Args:
        user: User object
        topic: Topic object
        profile: UserProfile object (optional)
    
    Returns:
        dict with 'xp_awarded', 'first_study' keys
    """
    from mainapp.models import TopicProgress
    
    # Check if this is the first time studying this topic
    progress, created = TopicProgress.objects.get_or_create(
        user=user,
        topic=topic,
        defaults={'status': 'not_started'}
    )
    
    if created or progress.status == 'not_started':
        # First time studying this topic
        progress.status = 'in_progress'
        progress.last_accessed = timezone.now()
        progress.save()
        
        result = award_xp(user, XP_REWARD_STUDY_TOPIC, f'Studied: {topic.name}', profile)
        return {
            'xp_awarded': result['xp_awarded'],
            'first_study': True,
            'leveled_up': result.get('leveled_up', False)
        }
    
    # Update last accessed
    progress.last_accessed = timezone.now()
    progress.save()
    
    return {'xp_awarded': 0, 'first_study': False}


def award_quiz_pass_xp(user, topic, percentage, profile=None):
    """
    Award XP for passing a quiz (>=70%).
    
    Args:
        user: User object
        topic: Topic object
        percentage: Quiz score percentage
        profile: UserProfile object (optional)
    
    Returns:
        dict with 'xp_awarded', 'passed' keys
    """
    if percentage >= 70:
        result = award_xp(user, XP_REWARD_QUIZ_PASS, f'Quiz passed: {topic.name}', profile)
        return {
            'xp_awarded': result['xp_awarded'],
            'passed': True,
            'leveled_up': result.get('leveled_up', False)
        }
    
    return {'xp_awarded': 0, 'passed': False}

def remove_quiz_data_for_topic(topic, delete_ai_quizzes=False):
    """
    Remove quiz data (Question and Choice) for a specific topic.
    
    Args:
        topic: Topic object to remove quiz data for
        delete_ai_quizzes: If True, also delete AI-generated quizzes for this topic
    
    Returns:
        dict with 'questions_deleted' count and 'ai_quizzes_deleted' count
    """
    from mainapp.models import Question, Choice, AIGeneratedQuiz
    
    result = {
        'questions_deleted': 0,
        'ai_quizzes_deleted': 0
    }
    
    # Delete topic-specific questions
    topic_questions = Question.objects.filter(topic=topic)
    for question in topic_questions:
        Choice.objects.filter(question=question).delete()
    result['questions_deleted'] += topic_questions.count()
    topic_questions.delete()
    
    # Delete section-level fallback questions for this topic's section
    section_questions = Question.objects.filter(section=topic.section, topic__isnull=True)
    for question in section_questions:
        Choice.objects.filter(question=question).delete()
    result['questions_deleted'] += section_questions.count()
    section_questions.delete()
    
    # Optionally delete AI-generated quizzes
    if delete_ai_quizzes:
        ai_quizzes = AIGeneratedQuiz.objects.filter(topic=topic)
        result['ai_quizzes_deleted'] = ai_quizzes.count()
        ai_quizzes.delete()
    
    return result


def find_broken_quizzes():
    """
    Find quizzes with broken options (questions with less than 2 options or no correct answer).
    
    Returns:
        dict with list of broken quiz IDs and details
    """
    from mainapp.models import AIGeneratedQuiz, AIGeneratedQuestion, AIGeneratedChoice
    
    broken_quizzes = []
    
    # Check all AI-generated quizzes
    for quiz in AIGeneratedQuiz.objects.all().prefetch_related('questions__choices'):
        quiz_issues = []
        
        for question in quiz.questions.all():
            choices = list(question.choices.all())
            
            # Check if question has less than 2 options
            if len(choices) < 2:
                quiz_issues.append({
                    'question_id': question.id,
                    'issue': f'Only {len(choices)} option(s)'
                })
                continue
            
            # Check if no option is marked as correct
            has_correct = any(c.is_correct for c in choices)
            if not has_correct:
                quiz_issues.append({
                    'question_id': question.id,
                    'issue': 'No correct answer marked'
                })
        
        if quiz_issues:
            broken_quizzes.append({
                'quiz_id': quiz.id,
                'quiz_title': quiz.title,
                'topic': quiz.topic.name if quiz.topic else None,
                'issues': quiz_issues
            })
    
    return broken_quizzes


def delete_broken_quizzes(quiz_ids=None):
    """
    Delete broken quizzes.
    
    Args:
        quiz_ids: List of quiz IDs to delete. If None, will find and delete all broken quizzes.
    
    Returns:
        dict with count of deleted quizzes
    """
    from mainapp.models import AIGeneratedQuiz
    
    if quiz_ids is None:
        # Find all broken quizzes
        broken = find_broken_quizzes()
        quiz_ids = [b['quiz_id'] for b in broken]
    
    deleted_count = 0
    for quiz_id in quiz_ids:
        try:
            quiz = AIGeneratedQuiz.objects.get(id=quiz_id)
            quiz.delete()
            deleted_count += 1
        except AIGeneratedQuiz.DoesNotExist:
            pass
    
    return {'deleted_count': deleted_count}


# Note: AI quiz generation for general content has been moved to teacher-only functionality.
# Teachers can use the AI Quiz Generator to create quizzes from study materials.
# See mainapp/views/ai_quiz_generator_views.py for teacher AI quiz functionality.



from django.contrib.auth.models import User
import random

# Home
def home(request):
    # Only show general sections (Environment, Heritage, Cultural) on home page
    general_sections = Section.objects.filter(is_general=True)
    return render(request, 'home.html', {'general_sections': general_sections})


# Profile view - redirects to appropriate dashboard based on user role
@login_required
def profile(request):
    """User profile view - redirects to role-specific dashboard"""
    try:
        user_profile = UserProfile.objects.get(user=request.user)
        if user_profile.role == 'admin':
            return redirect('admin_dashboard')
        elif user_profile.role == 'teacher':
            return redirect('teacher_dashboard')
        else:
            return redirect('student_dashboard')
    except UserProfile.DoesNotExist:
        messages.error(request, 'Profile not found.')
        return redirect('login')


# Class view - redirects teachers to manage classes page
@login_required
def class_view(request):
    """Class management view for teachers"""
    try:
        user_profile = UserProfile.objects.get(user=request.user)
        if user_profile.role == 'teacher' or user_profile.role == 'admin':
            return redirect('teacher_manage_classes')
        else:
            # Students don't have access to class management, redirect to their dashboard with message
            messages.info(request, 'You do not have access to class management.')
            return redirect('student_dashboard')
    except UserProfile.DoesNotExist:
        messages.error(request, 'Profile not found.')
        return redirect('login')


# Environment
def environment(request):
    section = get_object_or_404(Section, name='Environment')
    # Get all root topics (no parent) for the Environment section
    topics = section.topics.filter(parent_topic__isnull=True).order_by('order')
    
    return render(request, 'environment.html', {'section': section, 'topics': topics})

def environment_topics(request):
    """Show topics list for Environment section"""
    section = get_object_or_404(Section, name='Environment')
    topics = section.topics.all().order_by('order')
    return render(request, 'environment_topics.html', {'section': section, 'topics': topics})

def topic_study(request, topic_id):
    """Show study page for a specific topic with text, image, audio"""
    topic = get_object_or_404(Topic, id=topic_id)
    study_materials = topic.study_materials.all().order_by('order')
    
    # Check for children (for the Hub/Premium layout)
    children = topic.sub_topics.all().order_by('order')
    
    # Award XP for studying this topic (for authenticated students)
    xp_info = None
    quiz_locked = True
    progress = None
    
    if request.user.is_authenticated:
        try:
            profile = UserProfile.objects.get(user=request.user)
            if profile.role == 'student':
                xp_result = award_study_xp(request.user, topic, profile)
                if xp_result['xp_awarded'] > 0:
                    xp_info = {
                        'xp': xp_result['xp_awarded'],
                        'reason': f'Studied: {topic.name}',
                        'leveled_up': xp_result.get('leveled_up', False)
                    }
                # Check explicit Study Progress
                progress = TopicProgress.objects.filter(user=request.user, topic=topic).first()
                if progress and progress.is_studied:
                    quiz_locked = False
                
                # If it's a hub topic, check children progress for Knowledge Bloom
                if children.exists():
                    for child in children:
                        TopicProgress.objects.get_or_create(user=request.user, topic=child)
            else:
                # Admins/Teachers don't have locked quizzes
                quiz_locked = False
        except UserProfile.DoesNotExist:
            pass
    
    return render(request, 'topic_study.html', {
        'topic': topic, 
        'study_materials': study_materials,
        'children': children,
        'xp_info': xp_info,
        'quiz_locked': quiz_locked,
        'progress': progress
    })

@login_required
def mark_general_topic_studied(request, topic_id):
    """Mark a general topic as explicitly studied by the student"""
    if request.method == 'POST':
        try:
            profile = UserProfile.objects.get(user=request.user)
            if profile.role == 'student' or profile.role == 'admin':
                topic = get_object_or_404(Topic, id=topic_id)
                progress, created = TopicProgress.objects.get_or_create(
                    user=request.user,
                    topic=topic,
                    defaults={'status': 'in_progress'}
                )
                progress.is_studied = True
                progress.save()
                messages.success(request, f"You have successfully marked '{topic.name}' as studied. You may now take the knowledge assessment.")
        except UserProfile.DoesNotExist:
            pass
    
    return redirect('topic_study', topic_id=topic_id)


def topic_quiz(request, topic_id):
    """Quiz specific to a topic"""
    topic = get_object_or_404(Topic, id=topic_id)
    section = topic.section
    
    # Check if student has marked the topic as studied first
    if request.user.is_authenticated:
        try:
            profile = UserProfile.objects.get(user=request.user)
            if profile.role == 'student':
                progress = TopicProgress.objects.filter(user=request.user, topic=topic).first()
                if not progress or not progress.is_studied:
                    messages.warning(request, f"Please review the materials and mark '{topic.name}' as studied before attempting the assessment.")
                    return redirect('topic_study', topic_id=topic.id)
        except UserProfile.DoesNotExist:
            pass
    else:
        messages.info(request, "Please log in to save your progress and take the assessment.")
        return redirect('login')
    
    if request.method == 'POST':
        # Handle quiz submission
        questions = request.session.get('quiz_questions', [])
        score = 0
        total = len(questions)
        for q in questions:
            user_answer = request.POST.get(f'question_{q["id"]}')
            if user_answer and int(user_answer) == q['correct_choice']:
                score += 1
        
        # Calculate percentage
        percentage = (score / total * 100) if total > 0 else 0
        
        # Get user's class if logged in (using Enrollment model)
        user_obj = None
        class_obj = None
        user_id = 'anonymous'
        
        # Check if user is student - only students can take quizzes
        if request.user.is_authenticated:
            try:
                profile = UserProfile.objects.get(user=request.user)
                if profile.role != 'student':
                    messages.error(request, 'Only students can take quizzes.')
                    return redirect('home')
                user_obj = request.user
                user_id = request.user.username
                enrollment = Enrollment.objects.filter(student=request.user, is_active=True).first()
                if enrollment:
                    class_obj = enrollment.class_obj
            except UserProfile.DoesNotExist:
                messages.error(request, 'Profile not found.')
                return redirect('home')
        else:
            messages.error(request, 'Please login as a student to take quizzes.')
            return redirect('login')
        
        quiz_attempt = QuizAttempt.objects.create(
            section=section,
            topic=topic,
            class_obj=class_obj,
            user=user_obj,
            user_identifier=user_id,
            score=score,
            total_questions=total
        )
        
        # Award XP and update progress if passed
        xp_results = []
        if percentage >= 70:
            # Update TopicProgress
            progress, created = TopicProgress.objects.get_or_create(
                user=user_obj,
                topic=topic,
                defaults={'status': 'completed', 'is_studied': True}
            )
            if not created and progress.status != 'completed':
                progress.status = 'completed'
                progress.completed_at = timezone.now()
                progress.save()
            
            # Award Pass XP
            pass_result = award_quiz_pass_xp(user_obj, topic, percentage, profile)
            if pass_result['xp_awarded'] > 0:
                xp_results.append({
                    'xp': pass_result['xp_awarded'],
                    'reason': f'Passed Knowledge Quiz: {topic.name}',
                    'leveled_up': pass_result.get('leveled_up', False)
                })
            
            # Check Module Completion
            module_result = check_module_completion(user_obj, section)
            if module_result['xp_awarded'] > 0:
                xp_results.append({
                    'xp': module_result['xp_awarded'],
                    'reason': f'Mastered Module: {section.name}',
                    'leveled_up': module_result.get('leveled_up', False)
                })
        
        # Store result in session for display
        request.session['last_quiz_result'] = {
            'score': score,
            'total': total,
            'percentage': percentage,
            'topic_name': topic.name,
            'attempt_id': quiz_attempt.id,
            'xp_results': xp_results
        }
        
        return redirect('quiz_result')
    else:
        # Display quiz - For general content topics, use only static sample data (no AI quizzes)
        # For non-general topics, check AI-generated quizzes first, then fallback to static questions
        difficulty = request.GET.get('difficulty', 'easy')
        
        quiz_questions = []
        
        # For general content topics (Environment, Heritage, Cultural), use only static sample data
        if topic.is_general:
            # Use only static Question model for general content
            questions_qs = Question.objects.filter(topic=topic, difficulty=difficulty)
            if not questions_qs.exists():
                questions_qs = Question.objects.filter(section=section, topic__isnull=True, difficulty=difficulty)
            if not questions_qs.exists():
                questions_qs = Question.objects.filter(section=section, topic__isnull=True)
            if not questions_qs.exists():
                questions_qs = Question.objects.filter(section=section)
            
            questions = list(questions_qs)
            random.shuffle(questions)
            for q in questions[:10]:
                choices = list(q.choice_set.all())
                random.shuffle(choices)
                correct_choice = None
                for i, c in enumerate(choices, 1):
                    if c.is_correct:
                        correct_choice = i
                        break
                quiz_questions.append({
                    'id': q.id,
                    'text': q.question_text,
                    'choices': [(i, c.choice_text) for i, c in enumerate(choices, 1)],
                    'correct_choice': correct_choice,
                    'is_ai': False
                })
        else:
            # For non-general topics (class-specific), check AI-generated quizzes first
            ai_quiz = AIGeneratedQuiz.objects.filter(
                topic=topic, 
                status='approved'
            ).first()
            
            if ai_quiz:
                # Use AI-generated questions
                ai_questions = AIGeneratedQuestion.objects.filter(
                    quiz=ai_quiz, 
                    difficulty=difficulty
                )
                if not ai_questions.exists():
                    ai_questions = AIGeneratedQuestion.objects.filter(quiz=ai_quiz)
                
                for q in ai_questions[:10]:
                    choices = list(q.choices.all())
                    random.shuffle(choices)
                    correct_choice = None
                    for i, c in enumerate(choices, 1):
                        if c.is_correct:
                            correct_choice = i
                            break
                    quiz_questions.append({
                        'id': q.id,
                        'text': q.question_text,
                        'choices': [(i, c.choice_text) for i, c in enumerate(choices, 1)],
                        'correct_choice': correct_choice,
                        'is_ai': True
                    })
            
            # Fallback to old Question model if no AI quiz exists
            if not quiz_questions:
                questions_qs = Question.objects.filter(topic=topic, difficulty=difficulty)
                if not questions_qs.exists():
                    questions_qs = Question.objects.filter(section=section, topic__isnull=True, difficulty=difficulty)
                if not questions_qs.exists():
                    questions_qs = Question.objects.filter(section=section, topic__isnull=True)
                if not questions_qs.exists():
                    questions_qs = Question.objects.filter(section=section)
                
                questions = list(questions_qs)
                random.shuffle(questions)
                for q in questions[:10]:
                    choices = list(q.choice_set.all())
                    random.shuffle(choices)
                    correct_choice = None
                    for i, c in enumerate(choices, 1):
                        if c.is_correct:
                            correct_choice = i
                            break
                    quiz_questions.append({
                        'id': q.id,
                        'text': q.question_text,
                        'choices': [(i, c.choice_text) for i, c in enumerate(choices, 1)],
                        'correct_choice': correct_choice,
                        'is_ai': False
                    })
        
        request.session['quiz_questions'] = quiz_questions
        
        return render(request, 'quizzes/topic_quiz.html', {
            'topic': topic,
            'section': section,
            'questions': quiz_questions,
            'difficulty': difficulty
        })

def quiz_result(request):
    """Display quiz result/score page"""
    result = request.session.get('last_quiz_result')
    if not result:
        return redirect('home')
    
    # Get user profile for level info
    profile = None
    if request.user.is_authenticated:
        try:
            profile = UserProfile.objects.get(user=request.user)
        except UserProfile.DoesNotExist:
            pass

    # Clean up XP info for template - student_quiz_result.html expects 'xp_earned_info'
    xp_earned_info = None
    if result.get('xp_results') and len(result['xp_results']) > 0:
        # For simplicity, we'll use the first one or combine them
        xp_earned_info = result['xp_results'][0]

    return render(request, 'student/student_quiz_result.html', {
        'score': result['score'],
        'total': result['total'],
        'percentage': result['percentage'],
        'topic_name': result['topic_name'],
        'xp_earned_info': xp_earned_info,
        'profile': profile
    })

def environment_quiz(request):
    return quiz_view(request, 'Environment')

def heritage(request):
    section = get_object_or_404(Section, name='Heritage Sites')
    topics = section.topics.all().order_by('order')
    return render(request, 'heritage.html', {'section': section, 'topics': topics})

def heritage_quiz(request):
    return quiz_view(request, 'Heritage Sites')

def cultural(request):
    section = get_object_or_404(Section, name='Cultural Artforms')
    topics = section.topics.all().order_by('order')
    return render(request, 'cultural.html', {'section': section, 'topics': topics})

def cultural_quiz(request):
    return quiz_view(request, 'Cultural Artforms')

def folklore(request):
    """View to display detailed Kerala Folklore and Culture"""
    return render(request, 'folklore.html')

def quiz_view(request, section_name):
    section = get_object_or_404(Section, name=section_name)
    if request.method == 'POST':
        # Handle quiz submission
        questions = request.session.get('quiz_questions', [])
        score = 0
        total = len(questions)
        for q in questions:
            user_answer = request.POST.get(f'question_{q["id"]}')
            if user_answer and int(user_answer) == q['correct_choice']:
                score += 1
        
        # Get user's class if logged in (using Enrollment model)
        user_obj = None
        class_obj = None
        user_id = 'anonymous'
        if request.user.is_authenticated:
            user_obj = request.user
            user_id = request.user.username
            enrollment = Enrollment.objects.filter(student=request.user, is_active=True).first()
            if enrollment:
                class_obj = enrollment.class_obj

        # Store attempt
        QuizAttempt.objects.create(
            section=section,
            class_obj=class_obj,
            user=user_obj,
            user_identifier=user_id,
            score=score,
            total_questions=total
        )
        messages.success(request, f'Quiz completed! Your score: {score}/{total}')
        return redirect('home')  # Or to results page
    else:
        # Display quiz
        difficulty = request.GET.get('difficulty', 'easy')
        questions_qs = Question.objects.filter(section=section, difficulty=difficulty)
        if not questions_qs.exists():
            questions_qs = Question.objects.filter(section=section)  # Fallback
        questions = list(questions_qs)
        random.shuffle(questions)
        quiz_questions = []
        for q in questions[:10]:  # Limit to 10 questions
            choices = list(q.choice_set.all())
            random.shuffle(choices)
            correct_choice = None
            for i, c in enumerate(choices, 1):
                if c.is_correct:
                    correct_choice = i
                    break
            quiz_questions.append({
                'id': q.id,
                'text': q.question_text,
                'choices': [(i, c.choice_text) for i, c in enumerate(choices, 1)],
                'correct_choice': correct_choice
            })
        request.session['quiz_questions'] = quiz_questions
        # Map section names to template names
        template_mapping = {
            'Environment': 'quizzes/environment_quiz.html',
            'Heritage Sites': 'quizzes/heritage_quiz.html',
            'Cultural Artforms': 'quizzes/cultural_quiz.html'
        }
        template_name = template_mapping.get(section_name, f'quizzes/{section_name.lower().replace(" ", "_")}_quiz.html')
        return render(request, template_name, {
            'section': section,
            'questions': quiz_questions,
            'difficulty': difficulty
        })

def leaderboard(request):
    # Top scorers per section
    sections = Section.objects.all()
    leaderboard_data = {}
    for section in sections:
        attempts = QuizAttempt.objects.filter(section=section).order_by('-score', 'date_attempted')[:10]
        leaderboard_data[section.name] = attempts
    # Overall ranking (sum of scores across sections)
    overall = {}
    for attempt in QuizAttempt.objects.all():
        uid = attempt.user_identifier
        if uid not in overall:
            overall[uid] = {'total_score': 0, 'attempts': 0}
        overall[uid]['total_score'] += attempt.score
        overall[uid]['attempts'] += 1
    overall_sorted = sorted(overall.items(), key=lambda x: x[1]['total_score'], reverse=True)[:10]
    return render(request, 'leaderboard.html', {
        'leaderboard_data': leaderboard_data,
        'overall': overall_sorted
    })

# Authentication views
def register(request):
    """Redirect to appropriate registration page"""
    return redirect('student_register')


def teacher_register(request):
    """Teacher registration view"""
    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            
            # Get additional fields
            email = request.POST.get('email', '')
            phone_number = request.POST.get('phone_number', '')
            position = request.POST.get('position', '')
            institution_type = request.POST.get('institution_type', 'existing')
            
            # Create teacher profile (using update_or_create to handle signals)
            profile, created = UserProfile.objects.update_or_create(
                user=user,
                defaults={
                    'role': 'teacher',
                    'email': email,
                    'phone_number': phone_number,
                    'position': position
                }
            )
            
            # Handle institution - either select existing or create new
            institution = None
            if institution_type == 'new':
                # Create new institution
                new_institution_name = request.POST.get('new_institution_name', '').strip()
                new_institution_description = request.POST.get('new_institution_description', '').strip()
                
                if new_institution_name:
                    institution, created = Institution.objects.get_or_create(
                        name=new_institution_name,
                        defaults={'description': new_institution_description}
                    )
                    if created:
                        messages.info(request, f'New institution "{new_institution_name}" has been created.')
            else:
                # Use existing institution
                institution_id = request.POST.get('institution')
                if institution_id:
                    try:
                        institution = Institution.objects.get(id=institution_id)
                    except Institution.DoesNotExist:
                        pass
            
            # Link to institution
            if institution:
                profile.institution = institution
                profile.save()
            
            messages.success(request, 'Teacher registration successful! Welcome to Eco Heritage.')
            login(request, user)
            return redirect('teacher_dashboard')
        else:
            messages.error(request, 'Please correct the errors below.')
    else:
        form = UserCreationForm()
    
    institutions = Institution.objects.all()
    
    return render(request, 'teacher/teacher_register.html', {
        'form': form,
        'institutions': institutions
    })



def student_register(request):
    """Student registration view"""
    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            
            # Get additional fields
            email = request.POST.get('email', '')
            phone_number = request.POST.get('phone_number', '')
            class_id = request.POST.get('student_class')
            
            # Create student profile (using update_or_create to handle signals)
            profile, created = UserProfile.objects.update_or_create(
                user=user,
                defaults={
                    'role': 'student',
                    'email': email,
                    'phone_number': phone_number
                }
            )
            
            # Link to class if provided (add to ManyToMany)
            if class_id:
                try:
                    student_class = Class.objects.get(id=class_id)
                    profile.student_class.add(student_class)
                except Class.DoesNotExist:
                    pass
            
            messages.success(request, 'Student registration successful! Welcome to Eco Heritage.')
            login(request, user)
            return redirect('student_dashboard')
        else:
            messages.error(request, 'Please correct the errors below.')
    else:
        form = UserCreationForm()
    
    classes = Class.objects.filter(is_active=True).select_related('institution')
    
    return render(request, 'student/student_register.html', {
        'form': form,
        'classes': classes
    })


def forgot_password(request):
    """Forgot password view"""
    if request.method == 'POST':
        username = request.POST.get('username')
        email = request.POST.get('email')
        role = request.POST.get('role', 'student')
        
        try:
            # Find user by username
            user = User.objects.get(username=username)
            
            # Check if user has the specified role
            profile = UserProfile.objects.get(user=user, role=role)
            
            # Verify email matches
            if profile.email == email:
                # In a real application, you would:
                # 1. Generate a password reset token
                # 2. Send an email with reset link
                # 3. Store the token in database
                
                # For this implementation, we'll generate a temporary password
                import random
                import string
                temp_password = ''.join(random.choices(string.ascii_letters + string.digits, k=10))
                
                # Set the new password
                user.set_password(temp_password)
                user.save()
                
                messages.success(
                    request, 
                    f'Password reset successful! Your temporary password is: {temp_password}. '
                    'Please login and change your password immediately.'
                )
                return redirect('login')
            else:
                messages.error(request, 'Email address does not match our records.')
                
        except User.DoesNotExist:
            messages.error(request, 'No user found with this username.')
        except UserProfile.DoesNotExist:
            messages.error(request, f'No {role} account found with this username.')
    
    return render(request, 'forgot_password.html')




def login_view(request):
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')
        
        if not username or not password:
            messages.error(request, 'Please enter both username and password.')
            return render(request, 'login.html')
        
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            messages.success(request, f'Welcome back, {username}!')
            return redirect('dashboard')
        else:
            messages.error(request, 'Invalid username or password. Please try again.')
    return render(request, 'login.html')


def logout_view(request):
    logout(request)
    return redirect('home')

@login_required
def dashboard(request):
    try:
        profile = UserProfile.objects.get(user=request.user)
    except UserProfile.DoesNotExist:
        messages.error(request, 'Profile not found. Please contact administrator.')
        return redirect('login')
    
    context = {'profile': profile}
    if profile.role == 'admin':
        # Redirect admin to admin dashboard
        return redirect('admin_dashboard')
    elif profile.role == 'teacher':
        # Redirect to teacher dashboard
        return redirect('teacher_dashboard')
    else:
        # Redirect to student dashboard
        return redirect('student_dashboard')
    # For student, basic dashboard
    return render(request, 'dashboard.html', context)


# ============================================
# TEACHER DASHBOARD VIEWS
# ============================================

@login_required
def teacher_dashboard(request):
    """Main teacher dashboard view"""
    # Check if user is teacher ONLY (admin should use admin_dashboard)
    try:
        profile = UserProfile.objects.get(user=request.user)
        # FIX Issue 19: Admin should not access teacher dashboard, only teachers
        if profile.role != 'teacher':
            # Redirect admins to admin dashboard, students to student dashboard
            if profile.role == 'admin':
                return redirect('admin_dashboard')
            return redirect('student_dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    # Get teacher's classes
    managed_classes = Class.objects.filter(teacher=request.user).select_related('institution')
    # Get students in teacher's classes
    total_students = UserProfile.objects.filter(
        role='student',
        student_class__in=managed_classes
    ).count()
    
    # Get statistics
    total_sections = Section.objects.count()
    total_topics = Topic.objects.count()
    total_questions = Question.objects.count()
    
    # Get recent quiz attempts from students in teacher's classes
    student_usernames = UserProfile.objects.filter(
        role='student',
        student_class__in=managed_classes
    ).values_list('user__username', flat=True)
    recent_attempts = QuizAttempt.objects.filter(
        user_identifier__in=list(student_usernames)
    ).order_by('-date_attempted')[:10]
    
    # --- Prepare Chart Data ---
    
    # 1. Class Size Distribution (Bar Chart)
    class_names = []
    class_sizes = []
    for c in managed_classes:
        class_names.append(c.name)
        class_sizes.append(c.students.count())
        
    # 2. Average Quiz Scores by Section (Radar or Bar Chart)
    from django.db.models import Avg
    section_scores = QuizAttempt.objects.filter(
        user_identifier__in=list(student_usernames)
    ).values('section__name').annotate(avg_score=Avg('score'))
    
    score_labels = [item['section__name'] for item in section_scores]
    score_data = [round(item['avg_score'], 1) if item['avg_score'] else 0 for item in section_scores]
    
    context = {
        'profile': profile,
        'managed_classes': managed_classes,
        'total_sections': total_sections,
        'total_topics': total_topics,
        'total_students': total_students,
        'total_questions': total_questions,
        'recent_attempts': recent_attempts,
        'class_names_json': class_names,
        'class_sizes_json': class_sizes,
        'score_labels_json': score_labels,
        'score_data_json': score_data,
    }
    return render(request, 'teacher/teacher_dashboard.html', context)



@login_required
def teacher_manage_sections(request):
    """Manage sections - add/edit/delete"""
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'teacher' and profile.role != 'admin':
            return redirect('student_dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    if request.method == 'POST':
        # Add new section
        name = request.POST.get('name')
        description = request.POST.get('description')
        image_url = request.POST.get('image_url', '')
        video_url = request.POST.get('video_url', '')
        
        if name and description:
            # Teachers can only create non-general sections
            # General sections can only be created/edited by admin
            Section.objects.create(
                name=name,
                description=description,
                image_url=image_url,
                video_url=video_url,
                is_general=False  # Teachers always create non-general sections
            )
            messages.success(request, f'Section "{name}" created successfully!')
        return redirect('teacher_manage_sections')
    
    # Admin can see all sections, teachers can only see non-general sections
    if profile.role == 'admin':
        sections = Section.objects.all()
    else:
        sections = Section.objects.filter(is_general=False)
    
    return render(request, 'teacher/teacher_manage_sections.html', {'sections': sections, 'profile': profile})


@login_required
def teacher_manage_topics(request, section_id=None):
    """Manage topics - add/edit/delete"""
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'teacher' and profile.role != 'admin':
            return redirect('student_dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    sections = Section.objects.all()
    
    if request.method == 'POST':
        # Add new topic
        section_id = request.POST.get('section')
        name = request.POST.get('name')
        description = request.POST.get('description')
        image_url = request.POST.get('image_url', '')
        audio_url = request.POST.get('audio_url', '')
        order = request.POST.get('order', 0)
        
        if section_id and name and description:
            section = Section.objects.get(id=section_id)
            Topic.objects.create(
                section=section,
                name=name,
                description=description,
                image_url=image_url,
                audio_url=audio_url,
                order=order
            )
            messages.success(request, f'Topic "{name}" created successfully!')
        return redirect('teacher_manage_topics')
    
    selected_section = None
    topics = []
    if section_id:
        selected_section = get_object_or_404(Section, id=section_id)
        topics = selected_section.topics.all().order_by('order')
    
    return render(request, 'teacher/teacher_manage_topics.html', {
        'sections': sections,
        'selected_section': selected_section,
        'topics': topics,
        'profile': profile
    })


@login_required
def teacher_manage_study_materials(request, topic_id=None):
    """Manage study materials - add/edit/delete"""
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'teacher' and profile.role != 'admin':
            return redirect('student_dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    sections = Section.objects.all()
    
    if request.method == 'POST':
        # Add new study material
        topic_id = request.POST.get('topic')
        title = request.POST.get('title')
        content_text = request.POST.get('content_text')
        image_url = request.POST.get('image_url', '')
        audio_url = request.POST.get('audio_url', '')
        video_url = request.POST.get('video_url', '')
        order = request.POST.get('order', 0)
        
        # Get uploaded files
        image_file = request.FILES.get('image')
        audio_file = request.FILES.get('audio')
        video_file = request.FILES.get('video')
        
        if topic_id and title and content_text:
            topic = Topic.objects.get(id=topic_id)
            StudyMaterial.objects.create(
                topic=topic,
                title=title,
                content_text=content_text,
                image_url=image_url,
                audio_url=audio_url,
                video_url=video_url,
                image=image_file,
                audio=audio_file,
                video=video_file,
                order=order
            )
            messages.success(request, f'Study material "{title}" created successfully!')
        return redirect('teacher_manage_study_materials')
    
    selected_topic = None
    study_materials = []
    if topic_id:
        selected_topic = get_object_or_404(Topic, id=topic_id)
        study_materials = selected_topic.study_materials.all().order_by('order')
    
    # Get all topics for dropdown
    all_topics = Topic.objects.all()
    
    return render(request, 'teacher/teacher_manage_study_materials.html', {
        'sections': sections,
        'all_topics': all_topics,
        'selected_topic': selected_topic,
        'study_materials': study_materials,
        'profile': profile
    })


@login_required
def teacher_add_quiz_question(request):
    """Add quiz questions with choices - supports multiple questions at once"""
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'teacher' and profile.role != 'admin':
            return redirect('student_dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    sections = Section.objects.all()
    topics = Topic.objects.all()
    
    if request.method == 'POST':
        section_id = request.POST.get('section')
        topic_id = request.POST.get('topic')
        
        if section_id:
            section = Section.objects.get(id=section_id)
            topic = Topic.objects.get(id=topic_id) if topic_id else None
            
            # Process multiple questions (up to 20)
            questions_created = 0
            
            for q_num in range(1, 21):
                question_text = request.POST.get(f'question_text_{q_num}', '').strip()
                if not question_text:
                    continue
                
                difficulty = request.POST.get(f'difficulty_{q_num}', 'easy')
                
                # Get choices
                choice1 = request.POST.get(f'q{q_num}_choice1', '').strip()
                choice2 = request.POST.get(f'q{q_num}_choice2', '').strip()
                choice3 = request.POST.get(f'q{q_num}_choice3', '').strip()
                choice4 = request.POST.get(f'q{q_num}_choice4', '').strip()
                correct_choice = request.POST.get(f'q{q_num}_correct', '1')
                
                if choice1 and choice2:
                    question = Question.objects.create(
                        section=section,
                        topic=topic,
                        question_text=question_text,
                        difficulty=difficulty
                    )
                    
                    # Create choices
                    Choice.objects.create(
                        question=question,
                        choice_text=choice1,
                        is_correct=(correct_choice == '1')
                    )
                    Choice.objects.create(
                        question=question,
                        choice_text=choice2,
                        is_correct=(correct_choice == '2')
                    )
                    if choice3:
                        Choice.objects.create(
                            question=question,
                            choice_text=choice3,
                            is_correct=(correct_choice == '3')
                        )
                    if choice4:
                        Choice.objects.create(
                            question=question,
                            choice_text=choice4,
                            is_correct=(correct_choice == '4')
                        )
                    questions_created += 1
            
            if questions_created > 0:
                messages.success(request, f'{questions_created} quiz question(s) created successfully!')
            else:
                messages.error(request, 'No questions were created. Please fill in at least one complete question.')
            
            return redirect('teacher_add_quiz_question')
    
    return render(request, 'teacher/teacher_add_quiz_question.html', {
        'sections': sections,
        'topics': topics,
        'profile': profile
    })


@login_required
def teacher_view_students(request):
    """View students in teacher's classes"""
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'teacher' and profile.role != 'admin':
            return redirect('student_dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    # Get teacher's classes
    if profile.role == 'admin':
        managed_classes = Class.objects.all()
    else:
        managed_classes = Class.objects.filter(teacher=request.user)
    
    # Get students in teacher's classes
    students = UserProfile.objects.filter(
        role='student',
        student_class__in=managed_classes
    ).select_related('user', 'student_class')
    
    return render(request, 'teacher/teacher_view_students.html', {
        'students': students,
        'profile': profile,
        'managed_classes': managed_classes
    })


@login_required
def teacher_class_detail(request, class_id):
    """View class details with tabs for sections, topics, materials, quiz, students, performance, leaderboard"""
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'teacher' and profile.role != 'admin':
            return redirect('student_dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    # Get the class
    if profile.role == 'admin':
        class_obj = get_object_or_404(Class, id=class_id)
    else:
        class_obj = get_object_or_404(Class, id=class_id, teacher=request.user)
    
    # Get sections for this class
    sections = Section.objects.filter(class_obj=class_obj)
    
    # Get all topics from class sections
    all_topics = Topic.objects.filter(section__in=sections)
    
    # Get all study materials from class topics
    all_materials = StudyMaterial.objects.filter(topic__in=all_topics)
    
    # Get all questions from class sections/topics
    all_questions = Question.objects.filter(section__in=sections) | Question.objects.filter(topic__in=all_topics)
    all_questions = all_questions.distinct()
    
    # Get students in this class
    students = UserProfile.objects.filter(role='student', student_class__in=[class_obj]).select_related('user')
    
    # Get quiz attempts from students in this class
    student_usernames = list(students.values_list('user__username', flat=True))
    quiz_attempts = QuizAttempt.objects.filter(user_identifier__in=student_usernames).order_by('-date_attempted')[:20]
    
    # Calculate performance stats
    total_quizzes = quiz_attempts.count()
    avg_score = 0
    best_score = 0
    if total_quizzes > 0:
        avg_score = sum(a.score for a in quiz_attempts) / total_quizzes
        best_score = max(a.score for a in quiz_attempts) if quiz_attempts else 0
    
    # Calculate total topics and questions
    total_topics = all_topics.count()
    total_questions = all_questions.count()
    
    # Build leaderboard - group by student and sum scores
    leaderboard_data = {}
    for attempt in quiz_attempts:
        uid = attempt.user_identifier
        if uid not in leaderboard_data:
            leaderboard_data[uid] = {'total_score': 0, 'total_attempts': 0}
        leaderboard_data[uid]['total_score'] += attempt.score
        leaderboard_data[uid]['total_attempts'] += 1
    
    # Get student objects for leaderboard
    leaderboard = []
    for username, data in leaderboard_data.items():
        try:
            student_user = User.objects.get(username=username)
            student_profile = UserProfile.objects.get(user=student_user, role='student')
            leaderboard.append({
                'student': student_profile,
                'total_score': data['total_score'],
                'total_attempts': data['total_attempts']
            })
        except (User.DoesNotExist, UserProfile.DoesNotExist):
            pass
    
    # Sort by total score
    leaderboard.sort(key=lambda x: x['total_score'], reverse=True)
    
    # Get AI-generated quizzes for this class
    ai_quizzes = AIGeneratedQuiz.objects.filter(class_obj=class_obj).select_related('topic', 'topic__section').prefetch_related('questions')
    
    context = {
        'class_obj': class_obj,
        'sections': sections,
        'all_topics': all_topics,
        'all_materials': all_materials,
        'all_questions': all_questions,
        'students': students,
        'quiz_attempts': quiz_attempts,
        'total_quizzes': total_quizzes,
        'avg_score': avg_score,
        'best_score': best_score,
        'total_topics': total_topics,
        'total_questions': total_questions,
        'leaderboard': leaderboard,
        'ai_quizzes': ai_quizzes,
        'profile': profile
    }
    
    return render(request, 'teacher/teacher_class_detail.html', context)


@login_required
def teacher_manage_classes(request):
    """Manage classes - create, edit, delete"""
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'teacher' and profile.role != 'admin':
            return redirect('student_dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    # Get available institutions for the form
    if profile.role == 'admin':
        institutions = Institution.objects.all()
        classes = Class.objects.all().select_related('institution', 'teacher')
    else:
        # Teachers can only see their institution's classes
        institutions = Institution.objects.filter(id=profile.institution.id) if profile.institution else Institution.objects.none()
        classes = Class.objects.filter(teacher=request.user).select_related('institution')
    
    if request.method == 'POST':
        action = request.POST.get('action')
        
        if action == 'create':
            name = request.POST.get('name')
            institution_id = request.POST.get('institution')
            
            if name and institution_id:
                try:
                    institution = Institution.objects.get(id=institution_id)
                    # Check if teacher belongs to this institution
                    if profile.role != 'admin' and profile.institution != institution:
                        messages.error(request, 'You can only create classes in your institution.')
                        return redirect('teacher_manage_classes')
                    
                    # Create the class - is_active=True so students can see and join it
                    subject = request.POST.get('subject', '')
                    class_obj = Class.objects.create(
                        name=name,
                        subject=subject,
                        institution=institution,
                        teacher=request.user,
                        is_active=True
                    )
                    messages.success(request, f'Class "{name}" created successfully!')
                    
                    # Check if we need to create content with the class
                    create_with_class = request.POST.get('create_with_class')
                    if create_with_class:
                        section_name = request.POST.get('section_name', '').strip()
                        section_description = request.POST.get('section_description', '').strip()
                        
                        if section_name:
                            # Create section linked to this class
                            section = Section.objects.create(
                                name=section_name,
                                description=section_description,
                                is_general=False,
                                class_obj=class_obj
                            )
                            messages.info(request, f'Section "{section_name}" created!')
                            
                            # Create topics (up to 5)
                            first_topic = None
                            for i in range(1, 6):
                                topic_name = request.POST.get(f'topic_name_{i}', '').strip()
                                topic_description = request.POST.get(f'topic_description_{i}', '').strip()
                                
                                if topic_name:
                                    topic = Topic.objects.create(
                                        section=section,
                                        name=topic_name,
                                        description=topic_description,
                                        is_general=False,
                                        order=i
                                    )
                                    if not first_topic:
                                        first_topic = topic
                                    messages.info(request, f'Topic "{topic_name}" created!')
                            
                            # Create quiz questions for first topic (up to 20)
                            if first_topic:
                                questions_created = 0
                                for q_num in range(1, 21):
                                    question_text = request.POST.get(f'question_text_{q_num}', '').strip()
                                    if question_text:
                                        difficulty = request.POST.get(f'difficulty_{q_num}', 'easy')
                                        
                                        # Get choices
                                        choice1 = request.POST.get(f'q{q_num}_choice1', '').strip()
                                        choice2 = request.POST.get(f'q{q_num}_choice2', '').strip()
                                        choice3 = request.POST.get(f'q{q_num}_choice3', '').strip()
                                        choice4 = request.POST.get(f'q{q_num}_choice4', '').strip()
                                        correct_choice = request.POST.get(f'q{q_num}_correct', '1')
                                        
                                        if choice1 and choice2:
                                            question = Question.objects.create(
                                                section=section,
                                                topic=first_topic,
                                                question_text=question_text,
                                                difficulty=difficulty
                                            )
                                            
                                            # Create choices
                                            Choice.objects.create(
                                                question=question,
                                                choice_text=choice1,
                                                is_correct=(correct_choice == '1')
                                            )
                                            Choice.objects.create(
                                                question=question,
                                                choice_text=choice2,
                                                is_correct=(correct_choice == '2')
                                            )
                                            if choice3:
                                                Choice.objects.create(
                                                    question=question,
                                                    choice_text=choice3,
                                                    is_correct=(correct_choice == '3')
                                                )
                                            if choice4:
                                                Choice.objects.create(
                                                    question=question,
                                                    choice_text=choice4,
                                                    is_correct=(correct_choice == '4')
                                                )
                                            questions_created += 1
                                
                                if questions_created > 0:
                                    messages.success(request, f'{questions_created} quiz question(s) created for {first_topic.name}!')
                    
                except Institution.DoesNotExist:
                    messages.error(request, 'Institution not found.')
        
        elif action == 'delete':
            class_id = request.POST.get('class_id')
            if class_id:
                try:
                    class_obj = Class.objects.get(id=class_id)
                    # Check if teacher owns this class
                    if profile.role != 'admin' and class_obj.teacher != request.user:
                        messages.error(request, 'You can only delete your own classes.')
                        return redirect('teacher_manage_classes')
                    
                    class_obj.delete()
                    messages.success(request, 'Class deleted successfully!')
                except Class.DoesNotExist:
                    messages.error(request, 'Class not found.')
        
        return redirect('teacher_manage_classes')
    
    context = {
        'classes': classes,
        'institutions': institutions,
        'profile': profile
    }
    return render(request, 'teacher/teacher_manage_classes.html', context)


@login_required
def teacher_add_student_to_class(request):
    """Add a student to a class"""
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'teacher' and profile.role != 'admin':
            return redirect('student_dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    # Get teacher's classes
    if profile.role == 'admin':
        managed_classes = Class.objects.all()
    else:
        managed_classes = Class.objects.filter(teacher=request.user)
    
    if request.method == 'POST':
        student_id = request.POST.get('student')
        class_id = request.POST.get('class')
        
        if student_id and class_id:
            try:
                student_profile = UserProfile.objects.get(user_id=student_id, role='student')
                class_obj = Class.objects.get(id=class_id)
                
                # Check if teacher owns this class
                if profile.role != 'admin' and class_obj.teacher != request.user:
                    messages.error(request, 'You can only add students to your own classes.')
                    return redirect('teacher_add_student_to_class')
                
                # Check if student belongs to same institution
                if profile.role != 'admin':
                    teacher_institution = profile.institution
                    class_institution = class_obj.institution
                    if teacher_institution != class_institution:
                        messages.error(request, 'You can only add students to classes in your institution.')
                        return redirect('teacher_add_student_to_class')
                
                student_profile.student_class.add(class_obj)
                messages.success(request, f'Student {student_profile.user.username} added to {class_obj.name}!')
                
            except UserProfile.DoesNotExist:
                messages.error(request, 'Student not found.')
            except Class.DoesNotExist:
                messages.error(request, 'Class not found.')
        
        return redirect('teacher_add_student_to_class')
    
    # Get students without a class or in teacher's classes
    if profile.role == 'admin':
        available_students = UserProfile.objects.filter(role='student').select_related('user')
    else:
        # Students in same institution without a class, or already in teacher's classes
        available_students = UserProfile.objects.filter(
            role='student',
            institution=profile.institution
        ).select_related('user')
    
    context = {
        'managed_classes': managed_classes,
        'available_students': available_students,
        'profile': profile
    }
    return render(request, 'teacher_add_students.html', context)



@login_required
def teacher_view_student_performance(request, student_id):
    """View specific student's performance"""
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'teacher' and profile.role != 'admin':
            return redirect('student_dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    student = get_object_or_404(User, id=student_id)
    student_profile = get_object_or_404(UserProfile, user=student, role='student')
    
    # Check if teacher can view this student (must be in teacher's class)
    if profile.role != 'admin':
        teacher_classes = Class.objects.filter(teacher=request.user)
        if student_profile.student_class not in teacher_classes:
            messages.error(request, 'You can only view performance of students in your classes.')
            return redirect('teacher_view_students')
    
    # Get student's quiz attempts
    quiz_attempts = QuizAttempt.objects.filter(user_identifier=student.username).order_by('-date_attempted')[:20]
    
    # Get student's marks (only from student's class)
    student_marks = StudentMarks.objects.filter(
        student=student,
        student_class=student_profile.student_class
    ).order_by('-date_assigned')
    
    # Calculate statistics
    total_quizzes = quiz_attempts.count()
    avg_score = 0
    if total_quizzes > 0:
        avg_score = sum(a.score for a in quiz_attempts) / total_quizzes
    
    context = {
        'student': student,
        'student_profile': student_profile,
        'quiz_attempts': quiz_attempts,
        'student_marks': student_marks,
        'total_quizzes': total_quizzes,
        'avg_score': avg_score,
        'profile': profile
    }
    return render(request, 'teacher_view_student_performance.html', context)



@login_required
def teacher_assign_marks(request, student_id=None):
    """Assign internal marks to students in teacher's classes"""
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'teacher' and profile.role != 'admin':
            return redirect('student_dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    sections = Section.objects.all()
    
    # Get students in teacher's classes only
    if profile.role == 'admin':
        students = UserProfile.objects.filter(role='student').select_related('user', 'institution')
        teacher_classes = Class.objects.all()
    else:
        teacher_classes = Class.objects.filter(teacher=request.user)
        students = UserProfile.objects.filter(
            role='student',
            student_class__in=teacher_classes
        ).select_related('user', 'institution')
    
    if request.method == 'POST':
        student_id = request.POST.get('student')
        section_id = request.POST.get('section')
        topic_id = request.POST.get('topic', None)
        marks = request.POST.get('marks')
        max_marks = request.POST.get('max_marks', 100)
        remarks = request.POST.get('remarks', '')
        
        if student_id and section_id and marks:
            student = User.objects.get(id=student_id)
            student_profile = UserProfile.objects.get(user=student, role='student')
            section = Section.objects.get(id=section_id)
            topic = Topic.objects.get(id=topic_id) if topic_id else None
            
            # Verify student is in teacher's class
            if profile.role != 'admin' and student_profile.student_class not in teacher_classes:
                messages.error(request, 'You can only assign marks to students in your classes.')
                return redirect('teacher_assign_marks')
            
            StudentMarks.objects.create(
                student=student,
                student_class=student_profile.student_class,
                section=section,
                topic=topic,
                marks=int(marks),
                max_marks=int(max_marks),
                remarks=remarks,
                assigned_by=request.user
            )
            messages.success(request, f'Marks assigned to {student.username} successfully!')
            return redirect('teacher_assign_marks')
    
    selected_student = None
    student_marks = []
    if student_id:
        selected_student = get_object_or_404(User, id=student_id)
        selected_profile = get_object_or_404(UserProfile, user=selected_student, role='student')
        
        # Check if teacher can view this student's marks
        if profile.role != 'admin' and selected_profile.student_class not in teacher_classes:
            messages.error(request, 'You can only view marks of students in your classes.')
            return redirect('teacher_assign_marks')
        
        student_marks = StudentMarks.objects.filter(
            student=selected_student,
            student_class=selected_profile.student_class
        ).order_by('-date_assigned')
    
    return render(request, 'teacher/teacher_assign_marks.html', {
        'sections': sections,
        'students': students,
        'selected_student': selected_student,
        'student_marks': student_marks,
        'profile': profile,
        'teacher_classes': teacher_classes
    })



# ============================================
# STUDENT PROFILE VIEWS
# ============================================

@login_required
def student_profile(request):
    """Display student profile page with picture and personal information"""
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'student' and profile.role != 'admin':
            return redirect('teacher_dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    # Get student's classes
    student_classes = profile.student_class.all()
    
    context = {
        'profile': profile,
        'student_classes': student_classes,
    }
    return render(request, 'student/student_profile.html', context)


@login_required
def edit_student_profile(request):
    """Edit student profile - picture and personal information"""
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'student' and profile.role != 'admin':
            return redirect('teacher_dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    if request.method == 'POST':
        # Update profile fields
        email = request.POST.get('email', '').strip()
        phone_number = request.POST.get('phone_number', '').strip()
        
        profile.email = email
        profile.phone_number = phone_number
        
        # Handle profile picture upload
        if request.FILES.get('image'):
            profile.image = request.FILES.get('image')
        
        profile.save()
        messages.success(request, 'Profile updated successfully!')
        return redirect('student_profile')
    
    context = {
        'profile': profile,
    }
    return render(request, 'student/edit_student_profile.html', context)


# ============================================
# STUDENT DASHBOARD VIEWS
# ============================================

@login_required
def student_dashboard(request):
    """Main student dashboard view"""
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'student' and profile.role != 'admin':
            return redirect('teacher_dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    # Import gamification models
    from mainapp.models import UserCollectible, UserArtifactShard, UserLeague
    
    # Get general sections (shown to all students)
    general_sections = Section.objects.filter(is_general=True)
    
    # Get class-specific sections (only shown if student is in a class)
    class_specific_sections = Section.objects.none()
    class_info = None
    student_classes = profile.student_class.all()
    if student_classes.exists():
        class_specific_sections = Section.objects.filter(class_obj__in=student_classes)
        
        # Get class information for the Class Information Card (use first class)
        class_obj = student_classes.first()
        if class_obj:
            total_students = UserProfile.objects.filter(role='student', student_class=class_obj).count()
            teacher_name = class_obj.teacher.get_full_name() or class_obj.teacher.username
            class_info = {
                'name': class_obj.name,
                'teacher_name': teacher_name,
                'subject': class_obj.subject,
                'total_students': total_students,
                'institution': class_obj.institution.name,
            }
    
    # Combine both for display (but track which is which)
    all_sections = list(general_sections) + list(class_specific_sections)
    
    # Get student's quiz attempts
    from django.db.models import Q
    quiz_attempts = QuizAttempt.objects.filter(
        Q(user=request.user) | Q(user_identifier=request.user.username)
    ).order_by('-date_attempted')[:5]
    
    # Get student's marks (only from their class)
    student_marks = StudentMarks.objects.filter(
        student=request.user,
        student_class__in=student_classes
    ).order_by('-date_assigned')[:5]
    
    # Calculate statistics
    total_quizzes = QuizAttempt.objects.filter(user_identifier=request.user.username).count()
    total_marks = StudentMarks.objects.filter(
        student=request.user,
        student_class__in=student_classes
    ).count()
    
    # Get AI recommendations data
    ai_recommendations = get_personalized_dashboard_data(request.user)
    weak_topics = ai_recommendations.get('weak_topics', [])
    recommended_materials = ai_recommendations.get('recommended_materials', [])
    
    # ============================================
    # KNOWLEDGE BLOOM - Skill Tree Data
    # ============================================
    # Get or create progress for all general topics
    general_topics = Topic.objects.filter(is_general=True).select_related('section', 'parent_topic')
    
    # Build the knowledge bloom tree structure
    knowledge_bloom = []
    for topic in general_topics.filter(parent_topic__isnull=True):  # Root topics
        topic_data = {
            'id': topic.id,
            'name': topic.name,
            'section': topic.section.name,
            'description': topic.description,
            'status': 'not_started',
            'quiz_percentage': 0,
            'is_unlocked': True,
            'children': []
        }
        
        # Get or create progress for this topic
        progress, created = TopicProgress.objects.get_or_create(
            user=request.user,
            topic=topic,
            defaults={'status': 'not_started'}
        )
        topic_data['status'] = progress.status
        topic_data['quiz_percentage'] = progress.quiz_percentage
        topic_data['is_unlocked'] = progress.is_unlocked()
        
        # Get children topics
        for child in topic.get_children():
            child_data = {
                'id': child.id,
                'name': child.name,
                'section': child.section.name,
                'description': child.description,
                'status': 'not_started',
                'quiz_percentage': 0,
                'is_unlocked': False,
                'children': []
            }
            
            child_progress, created = TopicProgress.objects.get_or_create(
                user=request.user,
                topic=child,
                defaults={'status': 'not_started'}
            )
            child_data['status'] = child_progress.status
            child_data['quiz_percentage'] = child_progress.quiz_percentage
            child_data['is_unlocked'] = child_progress.is_unlocked()
            
            # Get grandchildren
            for grandchild in child.get_children():
                grandchild_data = {
                    'id': grandchild.id,
                    'name': grandchild.name,
                    'section': grandchild.section.name,
                    'description': grandchild.description,
                    'status': 'not_started',
                    'quiz_percentage': 0,
                    'is_unlocked': False,
                    'children': []
                }
                
                grandchild_progress, created = TopicProgress.objects.get_or_create(
                    user=request.user,
                    topic=grandchild,
                    defaults={'status': 'not_started'}
                )
                grandchild_data['status'] = grandchild_progress.status
                grandchild_data['quiz_percentage'] = grandchild_progress.quiz_percentage
                grandchild_data['is_unlocked'] = grandchild_progress.is_unlocked()
                
                child_data['children'].append(grandchild_data)
            
            topic_data['children'].append(child_data)
        
        knowledge_bloom.append(topic_data)
    
    # Calculate overall progress
    total_topics = Topic.objects.filter(is_general=True).count()
    completed_topics = TopicProgress.objects.filter(
        user=request.user,
        status='completed'
    ).count()
    overall_progress = int((completed_topics / total_topics * 100)) if total_topics > 0 else 0
    
    # Update and get streak
    update_streak(profile)
    streak = profile.current_streak
    
    # Get gamification data
    level_title = get_level_title(profile.level)
    spirit_guide_message = get_spirit_guide_message(request.user, profile)
    
    # Get collectibles and shards
    user_collectibles = profile.collectibles.all().select_related('item')
    user_shards = profile.artifact_shards.all().select_related('shard__parent_artifact')
    
    # Get league status + leaderboard
    league_info = None
    league_leaderboard = []
    league_rank = None
    league_notifications = []
    try:
        user_league = UserLeague.objects.get(profile=profile)
        current_league = user_league.league
        league_info = {
            'name': current_league.name if current_league else "Unassigned",
            'points': user_league.points_this_week,
            'icon': current_league.icon if current_league else "fa-shield-halved",
        }
        # Build leaderboard for current league
        if current_league:
            league_leaderboard = calculate_weekly_leaderboard(league=current_league, limit=5)
            # Find current user's rank
            for entry in league_leaderboard:
                if entry['profile_id'] == profile.id:
                    league_rank = entry['rank']
                    break
            # If user not in top 5, calculate their actual rank
            if league_rank is None:
                full_board = calculate_weekly_leaderboard(league=current_league, limit=100)
                for entry in full_board:
                    if entry['profile_id'] == profile.id:
                        league_rank = entry['rank']
                        break
    except UserLeague.DoesNotExist:
        pass
    
    # Get league notifications
    league_notifications = list(get_unread_league_notifications(profile)[:3])

    # Challenge Mode Data
    from mainapp.models import ChallengeAttempt
    from django.db.models import Sum, Max, Count
    
    challenge_attempts = ChallengeAttempt.objects.filter(
        user=request.user, 
        is_completed=True
    ).order_by('-end_time')[:5]
    
    challenge_stats = ChallengeAttempt.objects.filter(
        user=request.user, 
        is_completed=True
    ).aggregate(
        total_points=Sum('score'),
        best_streak=Max('max_streak'),
        total_completed=Count('id')
    )

    context = {
        'profile': profile,
        'level_title': level_title,
        'spirit_guide_message': spirit_guide_message,
        'user_collectibles': user_collectibles,
        'user_shards': user_shards,
        'league_info': league_info,
        'league_leaderboard': league_leaderboard,
        'league_rank': league_rank,
        'league_notifications': league_notifications,
        'sections': all_sections,
        'general_sections': general_sections,
        'class_specific_sections': class_specific_sections,
        'quiz_attempts': quiz_attempts,
        'student_marks': student_marks,
        'total_quizzes': total_quizzes,
        'total_marks': total_marks,
        'student_classes': student_classes,
        'class_info': class_info,
        'weak_topics': weak_topics,
        'recommended_materials': recommended_materials,
        'knowledge_bloom': knowledge_bloom,
        'overall_progress': overall_progress,
        'streak': streak,
        'challenge_attempts': challenge_attempts,
        'challenge_stats': challenge_stats,
        'total_challenge_points': challenge_stats['total_points'] or 0,
    }
    return render(request, 'student/student_dashboard.html', context)


@login_required
def dismiss_league_notification(request, notification_id):
    """AJAX endpoint to mark a league notification as read."""
    if request.method == 'POST':
        from mainapp.models import LeagueNotification
        try:
            profile = UserProfile.objects.get(user=request.user)
            notification = LeagueNotification.objects.get(id=notification_id, profile=profile)
            notification.is_read = True
            notification.save()
            return JsonResponse({'status': 'ok'})
        except (UserProfile.DoesNotExist, LeagueNotification.DoesNotExist):
            return JsonResponse({'status': 'error', 'message': 'Not found'}, status=404)
    return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)


@login_required
def student_view_topics(request):
    """View all topics for studying"""
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'student' and profile.role != 'admin':
            return redirect('teacher_dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')

    # Get class-specific sections (only shown if student is in a class)
    class_specific_sections = Section.objects.none()
    class_info = None
    student_classes = profile.student_class.all()
    if student_classes.exists():
        class_specific_sections = Section.objects.filter(class_obj__in=student_classes)

        # Get class information for the Class Information Card (use first class)
        class_obj = student_classes.first()
        if class_obj:
            total_students = UserProfile.objects.filter(role='student', student_class=class_obj).count()
            teacher_name = class_obj.teacher.get_full_name() or class_obj.teacher.username
            class_info = {
                'name': class_obj.name,
                'teacher_name': teacher_name,
                'subject': class_obj.subject,
                'total_students': total_students,
                'institution': class_obj.institution.name,
            }

    # Only display class-specific sections
    all_sections = list(class_specific_sections)

    return render(request, 'student/student_view_topics.html', {
        'sections': all_sections,
        'class_specific_sections': class_specific_sections,
        'profile': profile,
        'student_classes': student_classes
    })


@login_required
def student_study_topic(request, topic_id):
    """Study a specific topic"""
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'student' and profile.role != 'admin':
            return redirect('teacher_dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')

    topic = get_object_or_404(Topic, id=topic_id)
    study_materials = topic.study_materials.all().order_by('order')
    
    # Award XP for studying this topic (first time only)
    xp_info = None
    xp_result = award_study_xp(request.user, topic, profile)
    if xp_result['xp_awarded'] > 0:
        xp_info = {
            'xp': xp_result['xp_awarded'],
            'reason': f'Studied: {topic.name}',
            'leveled_up': xp_result.get('leveled_up', False),
            'first_study': xp_result.get('first_study', False)
        }
    
    # Also refresh the profile to get updated XP/level
    profile.refresh_from_db()

    # Determine if quiz is locked
    quiz_locked = True
    progress = TopicProgress.objects.filter(user=request.user, topic=topic).first()
    if progress and progress.is_studied:
        quiz_locked = False

    return render(request, 'student/student_study_topic.html', {
        'topic': topic,
        'study_materials': study_materials,
        'profile': profile,
        'xp_info': xp_info,
        'quiz_locked': quiz_locked,
        'progress': progress
    })

@login_required
def student_mark_topic_studied(request, topic_id):
    """Mark a topic as explicitly studied by the student"""
    if request.method == 'POST':
        try:
            profile = UserProfile.objects.get(user=request.user)
            if profile.role == 'student' or profile.role == 'admin':
                topic = get_object_or_404(Topic, id=topic_id)
                progress, created = TopicProgress.objects.get_or_create(
                    user=request.user,
                    topic=topic,
                    defaults={'status': 'in_progress'}
                )
                progress.is_studied = True
                progress.save()
                messages.success(request, f"You have successfully completed '{topic.name}'. You may now take the certification quiz!")
        except UserProfile.DoesNotExist:
            pass
    
    return redirect('student_study_topic', topic_id=topic_id)

@login_required
def student_take_quiz(request, topic_id):
    """Student takes a quiz for a specific topic"""
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'student' and profile.role != 'admin':
            return redirect('teacher_dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    topic = get_object_or_404(Topic, id=topic_id)
    section = topic.section
    
    # Check if student has explicitly marked the topic as studied
    progress = TopicProgress.objects.filter(user=request.user, topic=topic).first()
    if not progress or not progress.is_studied:
        messages.warning(request, f"Please review the materials and mark '{topic.name}' as complete before attempting the certification.")
        return redirect('student_study_topic', topic_id=topic.id)
    
    if request.method == 'POST':
        # Handle quiz submission
        questions = request.session.get('quiz_questions', [])
        score = 0
        total = len(questions)
        for q in questions:
            user_answer = request.POST.get(f'question_{q["id"]}')
            if user_answer and int(user_answer) == q['correct_choice']:
                score += 1
        
        # Calculate percentage
        percentage = (score / total * 100) if total > 0 else 0
        
        # Get user's class if logged in
        user_obj = request.user
        class_obj = None
        try:
            profile = UserProfile.objects.get(user=request.user)
            student_classes = profile.student_class.all()
            if student_classes.exists():
                class_obj = student_classes.first()
        except UserProfile.DoesNotExist:
            pass
        
        # Store attempt in database
        quiz_attempt = QuizAttempt.objects.create(
            section=section,
            topic=topic,
            class_obj=class_obj,
            user=user_obj,
            user_identifier=request.user.username,
            score=score,
            total_questions=total
        )
        
        # Award XP and update progress if passed
        xp_results = []
        if percentage >= 70:
            # Update TopicProgress
            from django.utils import timezone
            progress, created = TopicProgress.objects.get_or_create(
                user=user_obj,
                topic=topic,
                defaults={'status': 'completed', 'is_studied': True}
            )
            if not created and progress.status != 'completed':
                progress.status = 'completed'
                progress.completed_at = timezone.now()
                progress.save()
            
            # Award Pass XP
            pass_result = award_quiz_pass_xp(user_obj, topic, percentage, profile)
            if pass_result['xp_awarded'] > 0:
                xp_results.append({
                    'xp': pass_result['xp_awarded'],
                    'reason': f'Passed Knowledge Quiz: {topic.name}',
                    'leveled_up': pass_result.get('leveled_up', False)
                })
            
            # Check Module Completion
            module_result = check_module_completion(user_obj, section)
            if module_result['xp_awarded'] > 0:
                xp_results.append({
                    'xp': module_result['xp_awarded'],
                    'reason': f'Mastered Module: {section.name}',
                    'leveled_up': module_result.get('leveled_up', False)
                })

        # Store result in session for display
        request.session['last_quiz_result'] = {
            'score': score,
            'total': total,
            'percentage': percentage,
            'topic_name': topic.name,
            'attempt_id': quiz_attempt.id,
            'xp_results': xp_results
        }
        
        return redirect('student_quiz_result')
    
    # Display quiz
    difficulty = request.GET.get('difficulty', 'easy')
    
    # For general content topics (Environment, Heritage, Cultural), use only static sample data
    # For non-general topics (class-specific), check AI-generated quizzes first, then fallback to static questions
    quiz_questions = []
    
    if topic.is_general:
        # Use only static Question model for general content (no AI quizzes)
        questions_qs = Question.objects.filter(topic=topic, difficulty=difficulty)
        if not questions_qs.exists():
            questions_qs = Question.objects.filter(section=section, topic__isnull=True, difficulty=difficulty)
        if not questions_qs.exists():
            questions_qs = Question.objects.filter(section=section, topic__isnull=True)
        if not questions_qs.exists():
            questions_qs = Question.objects.filter(section=section)
        
        questions = list(questions_qs)
        random.shuffle(questions)
        for q in questions[:10]:
            choices = list(q.choice_set.all())
            random.shuffle(choices)
            correct_choice = None
            for i, c in enumerate(choices, 1):
                if c.is_correct:
                    correct_choice = i
                    break
            quiz_questions.append({
                'id': q.id,
                'text': q.question_text,
                'choices': [(i, c.choice_text) for i, c in enumerate(choices, 1)],
                'correct_choice': correct_choice,
                'is_ai': False
            })
    else:
        # For non-general topics (class-specific), check AI-generated quizzes first
        ai_quiz = AIGeneratedQuiz.objects.filter(
            topic=topic, 
            status='approved'
        ).first()
        
        if ai_quiz:
            # Use AI-generated questions
            ai_questions = AIGeneratedQuestion.objects.filter(
                quiz=ai_quiz, 
                difficulty=difficulty
            )
            if not ai_questions.exists():
                ai_questions = AIGeneratedQuestion.objects.filter(quiz=ai_quiz)
            
            for q in ai_questions[:10]:
                choices = list(q.choices.all())
                random.shuffle(choices)
                correct_choice = None
                for i, c in enumerate(choices, 1):
                    if c.is_correct:
                        correct_choice = i
                        break
                quiz_questions.append({
                    'id': q.id,
                    'text': q.question_text,
                    'choices': [(i, c.choice_text) for i, c in enumerate(choices, 1)],
                    'correct_choice': correct_choice,
                    'is_ai': True
                })
        
        # Fallback to old Question model if no AI quiz exists
        if not quiz_questions:
            questions_qs = Question.objects.filter(topic=topic, difficulty=difficulty)
            if not questions_qs.exists():
                questions_qs = Question.objects.filter(section=section, topic__isnull=True, difficulty=difficulty)
            if not questions_qs.exists():
                questions_qs = Question.objects.filter(section=section, topic__isnull=True)
            if not questions_qs.exists():
                questions_qs = Question.objects.filter(section=section)
            
            questions = list(questions_qs)
            random.shuffle(questions)
            for q in questions[:10]:
                choices = list(q.choice_set.all())
                random.shuffle(choices)
                correct_choice = None
                for i, c in enumerate(choices, 1):
                    if c.is_correct:
                        correct_choice = i
                        break
                quiz_questions.append({
                    'id': q.id,
                    'text': q.question_text,
                    'choices': [(i, c.choice_text) for i, c in enumerate(choices, 1)],
                    'correct_choice': correct_choice,
                    'is_ai': False
                })
    
    request.session['quiz_questions'] = quiz_questions
    
    return render(request, 'student/student_take_quiz.html', {
        'topic': topic,
        'questions': quiz_questions,
        'difficulty': difficulty,
        'profile': profile
    })


@login_required
def student_quiz_result(request):
    """Display quiz result for student"""
    try:
        profile = UserProfile.objects.get(user=request.user)
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    result = request.session.get('last_quiz_result')
    if not result:
        return redirect('student_dashboard')
    
    # XP info should already be in result['xp_results'] from student_take_quiz
    xp_earned_info = None
    if result.get('xp_results') and len(result['xp_results']) > 0:
        xp_earned_info = result['xp_results'][0]
    
    return render(request, 'student/student_quiz_result.html', {
        'score': result['score'],
        'total': result['total'],
        'percentage': result['percentage'],
        'topic_name': result['topic_name'],
        'profile': profile,
        'xp_earned_info': xp_earned_info
    })


@login_required
def student_view_marks(request):
    """View student's internal marks (only from their class)"""
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'student' and profile.role != 'admin':
            return redirect('teacher_dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    student_classes = profile.student_class.all()
    
    # Only show marks from student's class
    student_marks = StudentMarks.objects.filter(
        student=request.user,
        student_class__in=student_classes
    ).order_by('-date_assigned')
    
    # Calculate total marks
    total_obtained = sum(m.marks for m in student_marks)
    total_max = sum(m.max_marks for m in student_marks)
    
    context = {
        'student_marks': student_marks,
        'total_obtained': total_obtained,
        'total_max': total_max,
        'profile': profile,
        'student_classes': student_classes,
    }
    return render(request, 'student/student_marks.html', context)



@login_required
def student_performance(request):
    """Track student's overall performance"""
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'student' and profile.role != 'admin':
            return redirect('teacher_dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    # Get all quiz attempts
    from django.db.models import Q
    quiz_attempts = QuizAttempt.objects.filter(
        Q(user=request.user) | Q(user_identifier=request.user.username)
    ).order_by('-date_attempted')
    
    # Get all marks
    student_marks = StudentMarks.objects.filter(student=request.user).order_by('-date_assigned')
    
    # Calculate statistics
    total_quizzes = quiz_attempts.count()
    avg_score = 0
    if total_quizzes > 0:
        avg_score = sum(a.score for a in quiz_attempts) / total_quizzes
    
    # Per-section performance
    section_performance = {}
    for attempt in quiz_attempts:
        section_name = attempt.section.name
        if section_name not in section_performance:
            section_performance[section_name] = {'total': 0, 'count': 0, 'score': 0}
        section_performance[section_name]['total'] += attempt.total_questions
        section_performance[section_name]['score'] += attempt.score
        section_performance[section_name]['count'] += 1
    
    # Calculate percentages
    for section_name in section_performance:
        if section_performance[section_name]['count'] > 0 and section_performance[section_name]['total'] > 0:
            section_performance[section_name]['avg_percentage'] = (
                section_performance[section_name]['score'] / 
                section_performance[section_name]['total'] * 100
            )
        else:
            section_performance[section_name]['avg_percentage'] = 0
    
    context = {
        'quiz_attempts': quiz_attempts,
        'student_marks': student_marks,
        'total_quizzes': total_quizzes,
        'avg_score': avg_score,
        'section_performance': section_performance,
        'profile': profile
    }
    return render(request, 'student/student_performance.html', context)


# AJAX views for dynamic content
@login_required
def get_topics_by_section(request):
    """AJAX: Get topics for a section"""
    section_id = request.GET.get('section_id')
    if section_id:
        topics = Topic.objects.filter(section_id=section_id).order_by('order')
        data = [{'id': t.id, 'name': t.name} for t in topics]
        return JsonResponse(data, safe=False)
    return JsonResponse([], safe=False)


@login_required
def get_study_materials(request):
    """AJAX: Get study materials for a topic"""
    topic_id = request.GET.get('topic_id')
    if topic_id:
        study_materials = StudyMaterial.objects.filter(topic_id=topic_id).order_by('order')
        data = [{'id': sm.id, 'title': sm.title, 'content_text': sm.content_text} for sm in study_materials]
        return JsonResponse(data, safe=False)
    return JsonResponse([], safe=False)


# ============================================
# CHALLENGE MODE VIEWS
# ============================================

@login_required
def challenge_mode(request):
    """Challenge mode lobby - select challenge type and settings"""
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'student' and profile.role != 'admin':
            return redirect('teacher_dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    sections = Section.objects.all()
    
    # Get user's challenge stats
    challenge_stats = {
        'total_challenges': ChallengeAttempt.objects.filter(user=request.user).count(),
        'best_streak': ChallengeAttempt.objects.filter(user=request.user).aggregate(max_streak=models.Max('max_streak'))['max_streak'] or 0,
        'high_score': ChallengeAttempt.objects.filter(user=request.user).aggregate(max_score=models.Max('score'))['max_score'] or 0,
    }
    
    return render(request, 'challenge_mode.html', {
        'sections': sections,
        'profile': profile,
        'challenge_stats': challenge_stats
    })


@login_required
def start_challenge(request):
    """Initialize a new challenge session"""
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'student' and profile.role != 'admin':
            return redirect('teacher_dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    if request.method == 'POST':
        challenge_type = request.POST.get('challenge_type', 'timed') # Default to timed
        section_id = request.POST.get('section')
        time_limit = int(request.POST.get('time_limit', 15))
        question_count = int(request.POST.get('question_count', 10))
        
        # Mode-specific overrides
        total_time_limit = None
        current_difficulty = 'easy'
        
        if challenge_type == 'eco_rush':
            # Eco-Rush has a total time limit (3 mins = 180s)
            total_time_limit = int(request.POST.get('eco_rush_duration', 180))
            question_count = 100 # Unlimited questions conceptually, capped by time
        elif challenge_type == 'survival':
            # Survival ends at 3 strikes
            question_count = 100 # Capped by strikes
            time_limit = 20 # Slower than timed
        elif challenge_type == 'sprint':
            # Sprint is a fixed set of questions (10 or 20)
            question_count = int(request.POST.get('question_count', 10))
            time_limit = 30 # No rush on per-question timer
        
        # Create challenge attempt
        section = None
        if section_id:
            section = get_object_or_404(Section, id=section_id)
        
        challenge = ChallengeAttempt.objects.create(
            user=request.user,
            section=section,
            challenge_type=challenge_type,
            time_limit_per_question=time_limit,
            total_time_limit=total_time_limit,
            total_questions=question_count,
            current_difficulty=current_difficulty
        )
        
        # Initialize session tracking
        request.session['current_challenge_id'] = challenge.id
        request.session['challenge_question_index'] = 0
        request.session['answered_questions'] = [] # Track IDs to avoid repeats
        request.session['last_correct_time'] = None # For combos
        
        return redirect('challenge_question')
    
    return redirect('challenge_mode')


@login_required
def challenge_question(request):
    """Display current challenge question with timer"""
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'student' and profile.role != 'admin':
            return redirect('teacher_dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    challenge_id = request.session.get('current_challenge_id')
    if not challenge_id:
        return redirect('challenge_mode')
    
    challenge = ChallengeAttempt.objects.filter(id=challenge_id, user=request.user).first()
    if not challenge:
        return redirect('challenge_mode')
    
    if challenge.is_completed or not challenge.is_active:
        return redirect('challenge_results')
    
    # Get current question index
    question_index = request.session.get('challenge_question_index', 0)
    
    # Check if challenge is complete by question count (for fixed modes)
    if challenge.challenge_type not in ['eco_rush', 'survival'] and question_index >= challenge.total_questions:
        challenge.is_completed = True
        challenge.is_active = False
        challenge.end_time = timezone.now()
        challenge.save()
        return redirect('challenge_results')
    
    # Check for Eco-Rush time limit
    if challenge.challenge_type == 'eco_rush' and challenge.total_time_limit:
        elapsed = (timezone.now() - challenge.start_time).total_seconds()
        if elapsed >= challenge.total_time_limit:
            challenge.is_completed = True
            challenge.is_active = False
            challenge.end_time = timezone.now()
            challenge.save()
            return redirect('challenge_results')

    # Get or Create current ChallengeQuestion
    answered_ids = request.session.get('answered_questions', [])
    
    # Try to get existing question for this index
    current_cq = ChallengeQuestion.objects.filter(challenge=challenge, question_order=question_index + 1).first()
    
    if not current_cq:
        # Select next question dynamically
        questions_qs = Question.objects.all()
        if challenge.section:
            questions_qs = questions_qs.filter(section=challenge.section)
        
        # Filter by difficulty
        questions_qs = questions_qs.filter(difficulty=challenge.current_difficulty).exclude(id__in=answered_ids)
        
        # Fallback if no questions of current difficulty are left
        if not questions_qs.exists():
            questions_qs = Question.objects.all()
            if challenge.section:
                questions_qs = questions_qs.filter(section=challenge.section)
            questions_qs = questions_qs.exclude(id__in=answered_ids)
            
        if not questions_qs.exists():
            # Truly no questions left
            challenge.is_completed = True
            challenge.is_active = False
            challenge.end_time = timezone.now()
            challenge.save()
            return redirect('challenge_results')
            
        question = random.choice(list(questions_qs))
        current_cq = ChallengeQuestion.objects.create(
            challenge=challenge,
            question=question,
            question_order=question_index + 1
        )
    
    question = current_cq.question
    
    # Get choices
    choices = list(question.choice_set.all())
    random.shuffle(choices)
    
    # Calculate progress
    progress = {
        'current': question_index + 1,
        'total': challenge.total_questions if challenge.challenge_type not in ['eco_rush', 'survival'] else '∞',
        'percentage': int((question_index / challenge.total_questions) * 100) if challenge.total_questions > 0 else 0
    }
    
    return render(request, 'quizzes/challenge_quiz.html', {
        'challenge': challenge,
        'challenge_question': current_cq,
        'question': question,
        'choices': choices,
        'progress': progress,
        'profile': profile,
        'strikes_count': challenge.strikes_count,
        'total_time_limit': challenge.total_time_limit
    })


@login_required
@csrf_exempt
def submit_challenge_answer(request):
    """AJAX endpoint to submit answer and get next question"""
    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid method'}, status=405)
    
    challenge_id = request.session.get('current_challenge_id')
    if not challenge_id:
        return JsonResponse({'error': 'No active challenge'}, status=400)
    
    challenge = ChallengeAttempt.objects.filter(id=challenge_id, user=request.user).first()
    if not challenge:
        return JsonResponse({'error': 'Challenge not found'}, status=404)
    
    if challenge.is_completed or not challenge.is_active:
        return JsonResponse({
            'complete': True,
            'redirect': '/student/challenge/results/'
        })
    
    question_index = request.session.get('challenge_question_index', 0)
    
    # Get current question
    current_cq = ChallengeQuestion.objects.filter(challenge=challenge, question_order=question_index + 1).first()
    if not current_cq:
        return JsonResponse({'error': 'Question not found'}, status=404)
    
    # Get submitted answer
    choice_id = request.POST.get('choice_id')
    time_taken = float(request.POST.get('time_taken', 0))
    
    is_correct = False
    points_earned = 0
    xp_earned = 0
    combo_active = False
    
    if choice_id:
        try:
            selected_choice = Choice.objects.get(id=choice_id)
            current_cq.selected_choice = selected_choice
            current_cq.is_correct = selected_choice.is_correct
            current_cq.time_taken = time_taken
            current_cq.answered_at = timezone.now()
            
            is_correct = current_cq.is_correct
            
            if is_correct:
                # Calculate points with time bonus
                base_points = 10
                time_bonus = max(0, int((challenge.time_limit_per_question - time_taken) * 0.5))
                streak_bonus = min(challenge.current_streak * 2, 10)
                
                points_earned = base_points + time_bonus + streak_bonus
                
                # Combo Multiplier (1.5x if answered correctly in under 10s)
                last_correct_time = request.session.get('last_correct_time')
                now_ts = timezone.now().timestamp()
                
                if last_correct_time and (now_ts - last_correct_time) < 15 and time_taken < 10:
                    points_earned = int(points_earned * 1.5)
                    combo_active = True
                
                request.session['last_correct_time'] = now_ts
                
                challenge.score += points_earned
                challenge.correct_answers += 1
                challenge.current_streak += 1
                challenge.max_streak = max(challenge.max_streak, challenge.current_streak)
                
                # Award XP
                xp_earned = 20 # Base XP per correct answer in challenge
                if combo_active:
                    xp_earned = int(xp_earned * 1.5)
                
                challenge.total_xp_earned += xp_earned
                
                # Update UserProfile XP
                profile = UserProfile.objects.get(user=request.user)
                profile.xp += xp_earned
                profile.total_xp_earned += xp_earned
                # Check for level up
                if profile.xp >= profile.get_xp_for_next_level():
                    profile.level += 1
                profile.save()
            else:
                challenge.current_streak = 0
                challenge.strikes_count += 1
                request.session['last_correct_time'] = None

            current_cq.points_earned = points_earned
            current_cq.save()
            challenge.save()
            
            # Add to answered list in session
            answered_questions = request.session.get('answered_questions', [])
            answered_questions.append(current_cq.question.id)
            request.session['answered_questions'] = answered_questions
            
        except Choice.DoesNotExist:
            pass
    else:
        # Time out handled as incorrect
        challenge.current_streak = 0
        challenge.strikes_count += 1
        challenge.save()
        request.session['last_correct_time'] = None

    # Progressive Difficulty logic
    if is_correct:
        if challenge.score >= 10:
            challenge.current_difficulty = 'hard'
        elif challenge.score >= 5:
            challenge.current_difficulty = 'medium'
        challenge.save()

    # Move to next question index
    question_index += 1
    request.session['challenge_question_index'] = question_index
    
    # Termination conditions
    terminate = False
    
    # 1. Fixed question count (Timed, Sprint, Adaptive)
    if challenge.challenge_type not in ['eco_rush', 'survival'] and question_index >= challenge.total_questions:
        terminate = True
    
    # 2. Survival mode: 3 strikes
    if challenge.challenge_type == 'survival' and challenge.strikes_count >= 3:
        terminate = True
        
    # 3. Eco-Rush: Time limit (checked in challenge_question, but can check here too)
    if challenge.challenge_type == 'eco_rush' and challenge.total_time_limit:
        elapsed = (timezone.now() - challenge.start_time).total_seconds()
        if elapsed >= challenge.total_time_limit:
            terminate = True

    if terminate:
        challenge.is_completed = True
        challenge.is_active = False
        challenge.end_time = timezone.now()
        challenge.save()
        return JsonResponse({
            'complete': True,
            'redirect': '/student/challenge/results/',
            'correct': is_correct,
            'points': points_earned,
            'xp_earned': xp_earned,
            'strikes': challenge.strikes_count
        })
    
    # Not terminated, return next info (frontend will call challenge_question or we can send data here)
    # The current frontend does a redirect or uses data from here. I'll send next question data.
    
    # For now, let's just return success and let the frontend reload or use the data
    # I'll return the same structure as before but updated
    return JsonResponse({
        'complete': False,
        'correct': is_correct,
        'points': points_earned,
        'xp_earned': xp_earned,
        'streak': challenge.current_streak,
        'combo': combo_active,
        'strikes': challenge.strikes_count,
        'progress': {
            'current': question_index + 1,
            'total': challenge.total_questions if challenge.challenge_type not in ['eco_rush', 'survival'] else '∞',
            'percentage': int((question_index / challenge.total_questions) * 100) if challenge.total_questions > 0 else 0
        }
    })


@login_required
def challenge_results(request):
    """Display challenge completion results"""
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'student' and profile.role != 'admin':
            return redirect('teacher_dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    # Get the most recent completed challenge
    challenge = ChallengeAttempt.objects.filter(
        user=request.user,
        is_completed=True
    ).order_by('-end_time').first()
    
    if not challenge:
        return redirect('challenge_mode')
    
    # Calculate statistics
    challenge_questions = challenge.challenge_questions.all()
    questions_count = challenge_questions.count()
    total_time = sum(cq.time_taken for cq in challenge_questions if cq.time_taken)
    avg_time = total_time / questions_count if questions_count > 0 else 0
    
    # Get accuracy by difficulty
    accuracy_by_difficulty = {}
    for cq in challenge_questions:
        diff = cq.question.difficulty
        if diff not in accuracy_by_difficulty:
            accuracy_by_difficulty[diff] = {'correct': 0, 'total': 0}
        accuracy_by_difficulty[diff]['total'] += 1
        if cq.is_correct:
            accuracy_by_difficulty[diff]['correct'] += 1
    
    for diff in accuracy_by_difficulty:
        acc = accuracy_by_difficulty[diff]
        acc['percentage'] = (acc['correct'] / acc['total'] * 100) if acc['total'] > 0 else 0
    
    # Clear session
    session_keys = ['current_challenge_id', 'challenge_question_index', 'answered_questions', 'last_correct_time']
    for key in session_keys:
        if key in request.session:
            del request.session[key]
    
    return render(request, 'challenge_results.html', {
        'challenge': challenge,
        'challenge_questions': challenge_questions,
        'avg_time': round(avg_time, 1),
        'accuracy_by_difficulty': accuracy_by_difficulty,
        'profile': profile
    })


# ============================================
# CLASS JOIN REQUEST VIEWS
# ============================================

@login_required
def student_join_class(request):
    """Student views available classes and sends join request"""
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'student' and profile.role != 'admin':
            return redirect('teacher_dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    # If student already has a class, redirect to dashboard (now allows multiple classes)
    if profile.student_class.exists():
        messages.info(request, 'You are already a member of classes. You can join more classes.')
    
    # Get all active classes with related data
    classes = Class.objects.filter(is_active=True).select_related('institution', 'teacher')
    
    # Organize classes by institution, then by teacher
    classes_by_institution = {}
    for class_obj in classes:
        institution_name = class_obj.institution.name
        if institution_name not in classes_by_institution:
            classes_by_institution[institution_name] = {
                'institution': class_obj.institution,
                'teachers': {}
            }
        
        teacher_username = class_obj.teacher.username
        if teacher_username not in classes_by_institution[institution_name]['teachers']:
            classes_by_institution[institution_name]['teachers'][teacher_username] = {
                'teacher': class_obj.teacher,
                'classes': []
            }
        
        classes_by_institution[institution_name]['teachers'][teacher_username]['classes'].append(class_obj)
    
    # Get user's pending requests
    my_requests = ClassJoinRequest.objects.filter(student=request.user)
    pending_class_ids = list(my_requests.filter(status='pending').values_list('class_obj_id', flat=True))
    
    if request.method == 'POST':
        class_id = request.POST.get('class_id')
        message = request.POST.get('message', '')
        
        if class_id:
            try:
                class_obj = Class.objects.get(id=class_id, is_active=True)
                
                # Check if already has a request
                existing_request = ClassJoinRequest.objects.filter(
                    student=request.user,
                    class_obj=class_obj
                ).first()
                
                if existing_request:
                    if existing_request.status == 'pending':
                        messages.error(request, 'You already have a pending request for this class.')
                    elif existing_request.status == 'approved':
                        messages.error(request, 'You are already a member of this class.')
                    else:
                        # Re-submit as new request
                        existing_request.status = 'pending'
                        existing_request.message = message
                        existing_request.processed_date = None
                        existing_request.processed_by = None
                        existing_request.save()
                        messages.success(request, f'Your request to join {class_obj.name} has been resubmitted!')
                else:
                    # Create new request
                    ClassJoinRequest.objects.create(
                        student=request.user,
                        class_obj=class_obj,
                        message=message
                    )
                    messages.success(request, f'Your request to join {class_obj.name} has been sent! The teacher will review it shortly.')
                
                return redirect('student_my_requests')
                
            except Class.DoesNotExist:
                messages.error(request, 'Class not found.')
    
    context = {
        'classes_by_institution': classes_by_institution,
        'pending_class_ids': pending_class_ids,
        'profile': profile,
    }
    return render(request, 'student/student_join_class.html', context)


@login_required
def student_my_requests(request):
    """Student views their join requests"""
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'student' and profile.role != 'admin':
            return redirect('teacher_dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    # Get user's all requests
    my_requests = ClassJoinRequest.objects.filter(student=request.user).select_related('class_obj', 'class_obj__institution', 'class_obj__teacher', 'processed_by')
    
    context = {
        'my_requests': my_requests,
        'profile': profile,
    }
    return render(request, 'student/student_my_requests.html', context)


@login_required
def teacher_view_join_requests(request):
    """Teacher views pending join requests for their classes"""
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'teacher' and profile.role != 'admin':
            return redirect('student_dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    # Get teacher's classes
    if profile.role == 'admin':
        managed_classes = Class.objects.all()
    else:
        managed_classes = Class.objects.filter(teacher=request.user)
    
    # Get pending requests for teacher's classes
    pending_requests = ClassJoinRequest.objects.filter(
        class_obj__in=managed_classes,
        status='pending'
    ).select_related('student', 'class_obj', 'class_obj__institution')
    
    # Get all requests (for history)
    all_requests = ClassJoinRequest.objects.filter(
        class_obj__in=managed_classes
    ).select_related('student', 'class_obj', 'class_obj__institution', 'processed_by')[:50]
    
    context = {
        'pending_requests': pending_requests,
        'all_requests': all_requests,
        'profile': profile,
        'managed_classes': managed_classes,
    }
    return render(request, 'teacher/teacher_join_requests.html', context)


@login_required
def teacher_process_join_request(request, request_id):
    """Teacher approves or rejects a join request"""
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'teacher' and profile.role != 'admin':
            return redirect('student_dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    join_request = get_object_or_404(ClassJoinRequest, id=request_id)
    
    # Verify teacher owns this class
    if profile.role != 'admin':
        if join_request.class_obj.teacher != request.user:
            messages.error(request, 'You can only process requests for your own classes.')
            return redirect('teacher_view_join_requests')
    
    if request.method == 'POST':
        action = request.POST.get('action')
        
        if action in ['approve', 'reject']:
            join_request.status = 'approved' if action == 'approve' else 'rejected'
            join_request.processed_by = request.user
            join_request.processed_date = timezone.now()
            join_request.save()
            
            if action == 'approve':
                # Grant student access to the class
                try:
                    student_profile = UserProfile.objects.get(user=join_request.student, role='student')
                    student_profile.student_class.add(join_request.class_obj)
                    messages.success(request, f'Approved! {join_request.student.username} has been added to {join_request.class_obj.name}.')
                except UserProfile.DoesNotExist:
                    messages.error(request, 'Student profile not found.')
            else:
                messages.info(request, f'Rejected request from {join_request.student.username}.')
        
        return redirect('teacher_view_join_requests')
    
    context = {
        'join_request': join_request,
        'profile': profile,
    }
    return redirect('teacher_view_join_requests')


# ============================================
# ADMIN DASHBOARD VIEWS
# ============================================

@login_required
def admin_dashboard(request):
    """Main Admin dashboard with system-wide statistics"""
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'admin':
            return redirect('dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    # Get system-wide statistics
    total_institutions = Institution.objects.count()
    total_teachers = UserProfile.objects.filter(role='teacher').count()
    total_students = UserProfile.objects.filter(role='student').count()
    active_classes = Class.objects.filter(is_active=True).count()
    total_quiz_attempts = QuizAttempt.objects.count()
    pending_requests = ClassJoinRequest.objects.filter(status='pending').count()
    
    # Get recent activity
    recent_attempts = QuizAttempt.objects.all().order_by('-date_attempted')[:10]
    recent_joins = ClassJoinRequest.objects.filter(status='approved').order_by('-processed_date')[:10]
    
    from django.db.models import Count
    from django.utils import timezone
    from datetime import timedelta

    # Calculate growth data (real data for charts)
    # Get student registrations over the last 6 months
    today = timezone.now()
    six_months_ago = today - timedelta(days=180)
    
    # Simple monthly aggregation for the bar chart
    growth_data = []
    month_labels = []
    
    for i in range(5, -1, -1):
        month_start = (today.replace(day=1) - timedelta(days=30 * i)).replace(day=1)
        if i == 0:
            month_end = today
        else:
            month_end = (today.replace(day=1) - timedelta(days=30 * (i-1))).replace(day=1) - timedelta(days=1)
        
        count = UserProfile.objects.filter(role='student', user__date_joined__range=(month_start, month_end)).count()
        growth_data.append(count)
        month_labels.append(month_start.strftime('%b %Y'))

    # Role distribution for doughnut chart
    role_distribution = {
        'Students': total_students,
        'Teachers': total_teachers,
        'Admins': UserProfile.objects.filter(role='admin').count()
    }
    
    context = {
        'profile': profile,
        'total_institutions': total_institutions,
        'total_teachers': total_teachers,
        'total_students': total_students,
        'active_classes': active_classes,
        'total_quiz_attempts': total_quiz_attempts,
        'pending_requests': pending_requests,
        'recent_attempts': recent_attempts,
        'recent_joins': recent_joins,
        'student_growth_data': growth_data,
        'student_growth_labels': month_labels,
        'role_distribution_data': list(role_distribution.values()),
        'role_distribution_labels': list(role_distribution.keys()),
    }
    return render(request, 'admin/admin_dashboard.html', context)


@login_required
def admin_manage_institutions(request):
    """Manage institutions - Add/Edit/Delete"""
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'admin':
            return redirect('dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    if request.method == 'POST':
        action = request.POST.get('action')
        
        if action == 'create':
            name = request.POST.get('name')
            description = request.POST.get('description', '')
            if name:
                Institution.objects.create(name=name, description=description)
                messages.success(request, f'Institution "{name}" created!')
        
        elif action == 'delete':
            institution_id = request.POST.get('institution_id')
            if institution_id:
                try:
                    institution = Institution.objects.get(id=institution_id)
                    institution.delete()
                    messages.success(request, 'Institution deleted!')
                except Institution.DoesNotExist:
                    messages.error(request, 'Institution not found.')
        
        return redirect('admin_manage_institutions')
    
    institutions = Institution.objects.all().prefetch_related('classes', 'teachers')
    
    # Build institution tree with teachers and students
    institution_tree = []
    for inst in institutions:
        teachers = UserProfile.objects.filter(role='teacher', institution=inst)
        classes = Class.objects.filter(institution=inst)
        student_count = UserProfile.objects.filter(
            role='student',
            student_class__in=classes
        ).distinct().count()
        
        institution_tree.append({
            'institution': inst,
            'teachers': teachers,
            'classes': classes,
            'student_count': student_count
        })
    
    return render(request, 'admin/admin_manage_institutions.html', {
        'institution_tree': institution_tree,
        'profile': profile
    })


@login_required
def admin_manage_teachers(request):
    """Manage teachers - Approve/Suspend/Assign Institution"""
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'admin':
            return redirect('dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    if request.method == 'POST':
        action = request.POST.get('action')
        
        if action == 'assign_institution':
            teacher_id = request.POST.get('teacher_id')
            institution_id = request.POST.get('institution_id')
            if teacher_id and institution_id:
                try:
                    teacher_profile = UserProfile.objects.get(id=teacher_id, role='teacher')
                    institution = Institution.objects.get(id=institution_id)
                    teacher_profile.institution = institution
                    teacher_profile.save()
                    messages.success(request, f'Institution assigned to {teacher_profile.user.username}')
                except (UserProfile.DoesNotExist, Institution.DoesNotExist):
                    messages.error(request, 'Teacher or Institution not found.')
        
        elif action == 'suspend':
            teacher_id = request.POST.get('teacher_id')
            if teacher_id:
                try:
                    teacher_profile = UserProfile.objects.get(id=teacher_id, role='teacher')
                    teacher_profile.user.is_active = False
                    teacher_profile.user.save()
                    messages.success(request, f'Teacher {teacher_profile.user.username} suspended.')
                except UserProfile.DoesNotExist:
                    messages.error(request, 'Teacher not found.')
        
        elif action == 'activate':
            teacher_id = request.POST.get('teacher_id')
            if teacher_id:
                try:
                    teacher_profile = UserProfile.objects.get(id=teacher_id, role='teacher')
                    teacher_profile.user.is_active = True
                    teacher_profile.user.save()
                    messages.success(request, f'Teacher {teacher_profile.user.username} activated.')
                except UserProfile.DoesNotExist:
                    messages.error(request, 'Teacher not found.')
        
        return redirect('admin_manage_teachers')
    
    teachers = UserProfile.objects.filter(role='teacher').select_related('user', 'institution')
    institutions = Institution.objects.all()
    
    return render(request, 'admin/admin_manage_teachers.html', {
        'teachers': teachers,
        'institutions': institutions,
        'profile': profile
    })


@login_required
def admin_manage_students(request):
    """Manage students - View/Filter/Block/Reset"""
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'admin':
            return redirect('dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    institution_id = request.GET.get('institution')
    students = UserProfile.objects.filter(role='student').select_related('user')
    
    if institution_id:
        students = students.filter(
            student_class__institution_id=institution_id
        ).distinct()
    
    if request.method == 'POST':
        action = request.POST.get('action')
        
        if action == 'block':
            student_id = request.POST.get('student_id')
            if student_id:
                try:
                    student_profile = UserProfile.objects.get(id=student_id, role='student')
                    student_profile.user.is_active = False
                    student_profile.user.save()
                    messages.success(request, f'Student {student_profile.user.username} blocked.')
                except UserProfile.DoesNotExist:
                    messages.error(request, 'Student not found.')
        
        elif action == 'unblock':
            student_id = request.POST.get('student_id')
            if student_id:
                try:
                    student_profile = UserProfile.objects.get(id=student_id, role='student')
                    student_profile.user.is_active = True
                    student_profile.user.save()
                    messages.success(request, f'Student {student_profile.user.username} unblocked.')
                except UserProfile.DoesNotExist:
                    messages.error(request, 'Student not found.')
        
        elif action == 'reset_password':
            student_id = request.POST.get('student_id')
            if student_id:
                try:
                    student_profile = UserProfile.objects.get(id=student_id, role='student')
                    import random
                    import string
                    temp_password = ''.join(random.choices(string.ascii_letters + string.digits, k=10))
                    student_profile.user.set_password(temp_password)
                    student_profile.user.save()
                    messages.success(request, f'Password reset! Temp: {temp_password}')
                except UserProfile.DoesNotExist:
                    messages.error(request, 'Student not found.')
        
        return redirect('admin_manage_students')
    
    institutions = Institution.objects.all()
    
    return render(request, 'admin/admin_manage_students.html', {
        'students': students,
        'institutions': institutions,
        'selected_institution': institution_id,
        'profile': profile
    })


@login_required
def admin_manage_classes(request):
    """Manage all classes globally - View/Deactivate"""
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'admin':
            return redirect('dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    classes = Class.objects.all().select_related('institution', 'teacher')
    
    if request.method == 'POST':
        action = request.POST.get('action')
        
        if action == 'deactivate':
            class_id = request.POST.get('class_id')
            if class_id:
                try:
                    class_obj = Class.objects.get(id=class_id)
                    class_obj.is_active = False
                    class_obj.save()
                    messages.success(request, f'Class {class_obj.name} deactivated.')
                except Class.DoesNotExist:
                    messages.error(request, 'Class not found.')
        
        elif action == 'activate':
            class_id = request.POST.get('class_id')
            if class_id:
                try:
                    class_obj = Class.objects.get(id=class_id)
                    class_obj.is_active = True
                    class_obj.save()
                    messages.success(request, f'Class {class_obj.name} activated.')
                except Class.DoesNotExist:
                    messages.error(request, 'Class not found.')
        
        return redirect('admin_manage_classes')
    
    # Calculate performance metrics for each class
    class_data = []
    for cls in classes:
        student_usernames = list(UserProfile.objects.filter(
            role='student',
            student_class=cls
        ).values_list('user__username', flat=True))
        
        attempts = QuizAttempt.objects.filter(user_identifier__in=student_usernames)
        total_attempts = attempts.count()
        avg_score = attempts.aggregate(models.Avg('score'))['score__avg'] or 0
        student_count = UserProfile.objects.filter(role='student', student_class=cls).count()
        
        class_data.append({
            'class_obj': cls,
            'student_count': student_count,
            'total_attempts': total_attempts,
            'avg_score': round(avg_score, 1)
        })
    
    return render(request, 'admin/admin_manage_classes.html', {
        'class_data': class_data,
        'profile': profile
    })


@login_required
def admin_manage_general_content(request):
    """Manage general sections and topics (Environment, Heritage, Cultural)"""
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'admin':
            return redirect('dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    if request.method == 'POST':
        action = request.POST.get('action')
        
        if action == 'create_section':
            name = request.POST.get('name')
            description = request.POST.get('description', '')
            image_url = request.POST.get('image_url', '')
            video_url = request.POST.get('video_url', '')
            if name:
                Section.objects.create(
                    name=name,
                    description=description,
                    image_url=image_url,
                    video_url=video_url,
                    is_general=True
                )
                messages.success(request, f'General section "{name}" created!')
        
        elif action == 'delete_section':
            section_id = request.POST.get('section_id')
            if section_id:
                try:
                    section = Section.objects.get(id=section_id)
                    section.delete()
                    messages.success(request, 'Section deleted!')
                except Section.DoesNotExist:
                    messages.error(request, 'Section not found.')
        
        elif action == 'create_topic':
            section_id = request.POST.get('section')
            name = request.POST.get('name')
            description = request.POST.get('description', '')
            image_url = request.POST.get('image_url', '')
            audio_url = request.POST.get('audio_url', '')
            order = request.POST.get('order', 0)
            
            if section_id and name:
                section = Section.objects.get(id=section_id)
                topic = Topic.objects.create(
                    section=section,
                    name=name,
                    description=description,
                    image_url=image_url,
                    audio_url=audio_url,
                    order=order,
                    is_general=True
                )
                messages.success(request, f'Topic "{name}" created!')
        
        elif action == 'delete_topic':
            topic_id = request.POST.get('topic_id')
            if topic_id:
                try:
                    topic = Topic.objects.get(id=topic_id)
                    topic.delete()
                    messages.success(request, 'Topic deleted!')
                except Topic.DoesNotExist:
                    messages.error(request, 'Topic not found.')
        
        return redirect('admin_manage_general_content')
    
    # Get only general sections with topics and study materials
    general_sections = Section.objects.filter(is_general=True).prefetch_related(
        'topics',
        'topics__study_materials'
    )
    
    return render(request, 'admin/admin_manage_general_content.html', {
        'general_sections': general_sections,
        'profile': profile
    })


@login_required
def admin_analytics(request):
    """System-wide analytics and performance data"""
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'admin':
            return redirect('dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    # Institution performance ranking
    institutions = Institution.objects.all()
    institution_performance = []
    for inst in institutions:
        classes = Class.objects.filter(institution=inst)
        student_usernames = list(UserProfile.objects.filter(
            role='student',
            student_class__in=classes
        ).distinct().values_list('user__username', flat=True))
        
        attempts = QuizAttempt.objects.filter(user_identifier__in=student_usernames)
        total_attempts = attempts.count()
        avg_score = attempts.aggregate(models.Avg('score'))['score__avg'] or 0
        
        institution_performance.append({
            'institution': inst,
            'total_attempts': total_attempts,
            'avg_score': round(avg_score, 1)
        })
    
    institution_performance.sort(key=lambda x: x['avg_score'], reverse=True)
    
    # Top and lowest performing classes
    all_classes = Class.objects.filter(is_active=True).select_related('institution')
    class_performance = []
    for cls in all_classes:
        student_usernames = list(UserProfile.objects.filter(
            role='student',
            student_class=cls
        ).values_list('user__username', flat=True))
        
        attempts = QuizAttempt.objects.filter(user_identifier__in=student_usernames)
        avg_score = attempts.aggregate(models.Avg('score'))['score__avg'] or 0
        total_attempts = attempts.count()
        
        if total_attempts > 0:
            class_performance.append({
                'class_obj': cls,
                'avg_score': round(avg_score, 1),
                'total_attempts': total_attempts
            })
    
    class_performance.sort(key=lambda x: x['avg_score'], reverse=True)
    top_classes = class_performance[:5]
    lowest_classes = class_performance[-5:] if len(class_performance) > 5 else class_performance
    
    # Most attempted quizzes
    section_attempts = []
    for section in Section.objects.all():
        attempts = QuizAttempt.objects.filter(section=section)
        section_attempts.append({
            'section': section,
            'attempts': attempts.count()
        })
    section_attempts.sort(key=lambda x: x['attempts'], reverse=True)
    most_attempted = section_attempts[:5]
    
    # Average scores by section
    avg_by_section = []
    for section in Section.objects.all():
        attempts = QuizAttempt.objects.filter(section=section)
        avg = attempts.aggregate(models.Avg('score'))['score__avg'] or 0
        if attempts.count() > 0:
            avg_by_section.append({
                'section': section.name,
                'avg_score': round(avg, 1)
            })
    
    return render(request, 'admin/admin_analytics.html', {
        'institution_performance': institution_performance,
        'top_classes': top_classes,
        'lowest_classes': lowest_classes,
        'most_attempted': most_attempted,
        'avg_by_section': avg_by_section,
        'profile': profile
    })


@login_required
def admin_view_join_requests(request):
    """View all join requests across the system"""
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'admin':
            return redirect('dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    # Get all pending requests
    pending_requests = ClassJoinRequest.objects.filter(
        status='pending'
    ).select_related('student', 'class_obj', 'class_obj__institution', 'class_obj__teacher')
    
    # Get all requests for history
    all_requests = ClassJoinRequest.objects.all().select_related(
        'student', 'class_obj', 'class_obj__institution', 'class_obj__teacher', 'processed_by'
    )[:100]
    
    # Calculate statistics
    total_approved = ClassJoinRequest.objects.filter(status='approved').count()
    total_rejected = ClassJoinRequest.objects.filter(status='rejected').count()
    
    return render(request, 'admin/admin_view_join_requests.html', {
        'pending_requests': pending_requests,
        'all_requests': all_requests,
        'total_approved': total_approved,
        'total_rejected': total_rejected,
        'profile': profile
    })


@login_required
def admin_system_settings(request):
    """System settings and security controls"""
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'admin':
            return redirect('dashboard')
    except UserProfile.DoesNotExist:
        return redirect('login')
    
    if request.method == 'POST':
        action = request.POST.get('action')
        
        if action == 'deactivate_user':
            user_id = request.POST.get('user_id')
            if user_id:
                try:
                    user = User.objects.get(id=user_id)
                    user.is_active = False
                    user.save()
                    messages.success(request, f'User {user.username} deactivated.')
                except User.DoesNotExist:
                    messages.error(request, 'User not found.')
        
        elif action == 'activate_user':
            user_id = request.POST.get('user_id')
            if user_id:
                try:
                    user = User.objects.get(id=user_id)
                    user.is_active = True
                    user.save()
                    messages.success(request, f'User {user.username} activated.')
                except User.DoesNotExist:
                    messages.error(request, 'User not found.')
        
        elif action == 'reset_password':
            user_id = request.POST.get('user_id')
            if user_id:
                try:
                    user = User.objects.get(id=user_id)
                    import random
                    import string
                    temp_password = ''.join(random.choices(string.ascii_letters + string.digits, k=10))
                    user.set_password(temp_password)
                    user.save()
                    messages.success(request, f'Password reset for {user.username}! Temp: {temp_password}')
                except User.DoesNotExist:
                    messages.error(request, 'User not found.')
        
        elif action == 'lock_class':
            class_id = request.POST.get('class_id')
            if class_id:
                try:
                    class_obj = Class.objects.get(id=class_id)
                    class_obj.is_active = False
                    class_obj.save()
                    messages.success(request, f'Class {class_obj.name} locked.')
                except Class.DoesNotExist:
                    messages.error(request, 'Class not found.')
        
        elif action == 'unlock_class':
            class_id = request.POST.get('class_id')
            if class_id:
                try:
                    class_obj = Class.objects.get(id=class_id)
                    class_obj.is_active = True
                    class_obj.save()
                    messages.success(request, f'Class {class_obj.name} unlocked.')
                except Class.DoesNotExist:
                    messages.error(request, 'Class not found.')
        
        return redirect('admin_system_settings')
    
    # Get all users for management
    all_users = User.objects.all().select_related('userprofile')
    all_classes = Class.objects.all().select_related('institution', 'teacher')
    
    return render(request, 'admin/admin_system_settings.html', {
        'all_users': all_users,
        'all_classes': all_classes,
        'profile': profile
    })


# ============================================
# QUIZ DATA MANAGEMENT VIEWS
# ============================================

@login_required
def remove_sample_quiz_for_topic(request, topic_id):
    """
    Remove quiz data (Question and Choice) for a specific topic via POST.
    Note: AI quiz generation is now only available for teachers via the AI Quiz Generator.
    """
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'admin':
            return JsonResponse({'error': 'Permission denied'}, status=403)
    except UserProfile.DoesNotExist:
        return JsonResponse({'error': 'Profile not found'}, status=404)
    
    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid method'}, status=405)
    
    topic = get_object_or_404(Topic, id=topic_id)
    
    try:
        # Use the helper function
        result = remove_quiz_data_for_topic(topic, delete_ai_quizzes=False)
        
        return JsonResponse({
            'success': True,
            'questions_deleted': result['questions_deleted'],
            'message': f"Removed {result['questions_deleted']} questions"
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@login_required
def remove_sample_quiz_data(request):
    """
    Remove all sample quiz data (Question and Choice) from all topics.
    Admin only. Note: AI quiz generation is now only available for teachers via the AI Quiz Generator.
    """
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'admin':
            return JsonResponse({'error': 'Permission denied'}, status=403)
    except UserProfile.DoesNotExist:
        return JsonResponse({'error': 'Profile not found'}, status=404)
    
    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid method'}, status=405)
    
    try:
        # Get all topics
        topics = Topic.objects.all()
        
        total_questions_deleted = 0
        
        for topic in topics:
            result = remove_quiz_data_for_topic(topic, delete_ai_quizzes=False)
            total_questions_deleted += result['questions_deleted']
        
        return JsonResponse({
            'success': True,
            'total_questions_deleted': total_questions_deleted,
            'message': f"Removed {total_questions_deleted} questions from {topics.count()} topics"
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@login_required
def delete_ai_quiz_for_topic(request, topic_id):
    """
    Remove AI-generated quiz for a specific topic without deleting the topic.
    Admin only.
    """
    try:
        profile = UserProfile.objects.get(user=request.user)
        if profile.role != 'admin':
            return JsonResponse({'error': 'Permission denied'}, status=403)
    except UserProfile.DoesNotExist:
        return JsonResponse({'error': 'Profile not found'}, status=404)
    
    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid method'}, status=405)
    
    topic = get_object_or_404(Topic, id=topic_id)
    
    try:
        # Find AI-generated quizzes for this topic
        ai_quizzes = AIGeneratedQuiz.objects.filter(topic=topic)
        
        if not ai_quizzes.exists():
            return JsonResponse({
                'success': True,
                'quizzes_deleted': 0,
                'message': 'No AI quizzes found for this topic'
            })
        
        # Count quizzes to be deleted
        quizzes_deleted = ai_quizzes.count()
        
        # Delete the quizzes (this will cascade delete questions and choices)
        ai_quizzes.delete()
        
        return JsonResponse({
            'success': True,
            'quizzes_deleted': quizzes_deleted,
            'message': f'Successfully removed {quizzes_deleted} quiz(es) for "{topic.name}"'
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)
