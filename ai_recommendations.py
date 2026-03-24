"""
AI Recommendation Functions for EcoHeritage
These functions provide intelligent study material recommendations based on student performance

Enhanced with:
- Fallback difficulty logic
- Impact score calculation
- New user recommendations
- Concept-based recommendations
- Adaptive difficulty adjustment
"""

from django.db.models import Count
from mainapp.models import StudyMaterial, QuizAttempt, MaterialView, MaterialAttempt, ConceptTag


def get_weak_concepts(user):
    """
    Detect weak concepts based on quiz performance.
    Returns topics where the student has less than 50% accuracy.
    
    Returns:
        List of dicts with 'topic', 'accuracy', 'total_attempts', 'correct_answers'
    """
    attempts = QuizAttempt.objects.filter(user_identifier=user.username)
    topic_scores = {}
    
    for attempt in attempts:
        topic = attempt.topic
        if topic:
            if topic.id not in topic_scores:
                topic_scores[topic.id] = {
                    'total': 0, 
                    'correct': 0, 
                    'topic': topic,
                    'attempts': 0
                }
            topic_scores[topic.id]['total'] += attempt.total_questions
            topic_scores[topic.id]['correct'] += attempt.score
            topic_scores[topic.id]['attempts'] += 1
    
    weak_topics = []
    for topic_id, data in topic_scores.items():
        if data['total'] > 0:
            accuracy = (data['correct'] / data['total']) * 100
            if accuracy < 50:
                weak_topics.append({
                    'topic': data['topic'],
                    'accuracy': accuracy,
                    'total_attempts': data['attempts'],
                    'correct_answers': data['correct'],
                    'total_questions': data['total']
                })
    
    # Sort by accuracy (weakest first)
    weak_topics.sort(key=lambda x: x['accuracy'])
    
    return weak_topics


def calculate_adaptive_difficulty(user, topic):
    """
    Calculate appropriate difficulty based on recent performance trends.
    
    Returns:
        'beginner', 'intermediate', or 'advanced'
    """
    recent_attempts = QuizAttempt.objects.filter(
        user_identifier=user.username,
        topic=topic
    ).order_by('-date_attempted', '-id')[:5]
    
    if not recent_attempts.exists():
        return 'beginner'
    
    # Calculate average and trend
    scores = [(a.score / a.total_questions * 100) for a in recent_attempts if a.total_questions > 0]
    
    if not scores:
        return 'beginner'
    
    avg_score = sum(scores) / len(scores)
    trend = scores[0] - scores[-1] if len(scores) > 1 else 0
    
    if avg_score < 50:
        return 'beginner'
    elif avg_score < 75:
        # If improving, recommend intermediate; if declining, stay with beginner
        return 'intermediate' if trend >= 0 else 'beginner'
    else:
        # If improving, recommend advanced; if declining, step back to intermediate
        return 'advanced' if trend >= 0 else 'intermediate'


def get_recommended_materials(user, weak_topics):
    """
    Get recommended study materials based on weak concepts.
    Uses enhanced AI logic with fallback difficulty matching:
    
    - Accuracy < 50% → Beginner materials (fallback: any materials)
    - Accuracy 50-75% → Intermediate materials (fallback: beginner)
    - Accuracy > 75% → Advanced materials (fallback: intermediate)
    
    Returns:
        List of dicts with material, reason, difficulty, suggested_difficulty
    """
    recommended = []
    
    for weak in weak_topics:
        topic = weak['topic']
        accuracy = weak['accuracy']
        
        # Use adaptive difficulty calculation
        difficulty = calculate_adaptive_difficulty(user, topic)
        
        # Try exact match first
        materials = list(StudyMaterial.objects.filter(
            topic=topic,
            difficulty=difficulty
        )[:3])
        
        # Fallback logic: if no exact match, get any materials for this topic
        suggested_difficulty = difficulty
        if not materials:
            materials = list(StudyMaterial.objects.filter(topic=topic)[:3])
            if materials:
                suggested_difficulty = 'adaptive'
        
        for material in materials:
            recommended.append({
                'material': material,
                'reason': f"Weak topic: {topic.name} ({accuracy:.0f}% accuracy)",
                'difficulty': material.difficulty,
                'suggested_difficulty': suggested_difficulty,
                'is_fallback': suggested_difficulty == 'adaptive'
            })
    
    return recommended


def get_concept_based_recommendations(user, topic, exclude_topic=True, limit=5):
    """
    Recommend materials based on related concepts.
    Useful for cross-topic learning and concept reinforcement.
    
    Args:
        user: The user object
        topic: The Topic to find related concepts for
        exclude_topic: Whether to exclude materials from the same topic
        limit: Maximum number of recommendations
    
    Returns:
        List of related StudyMaterials
    """
    # Get concepts for the target topic
    topic_concepts = ConceptTag.objects.filter(
        study_materials__topic=topic
    ).distinct()
    
    if not topic_concepts.exists():
        return []
    
    # Build query for related materials
    query = StudyMaterial.objects.filter(
        concept_tags__in=topic_concepts
    ).distinct()
    
    if exclude_topic:
        query = query.exclude(topic=topic)
    
    return list(query[:limit])


def get_new_user_recommendations(limit=10):
    """
    Get recommendations for users with no quiz history.
    Uses popularity-based and recency-based recommendations.
    
    Returns:
        List of recommended StudyMaterials
    """
    # Get most viewed materials (popularity-based)
    popular_ids = MaterialView.objects.values('material__id').annotate(
        view_count=Count('id')
    ).order_by('-view_count')[:limit]
    
    popular_materials = []
    if popular_ids:
        material_ids = [item['material__id'] for item in popular_ids]
        popular_materials = list(StudyMaterial.objects.filter(id__in=material_ids))
    
    # If not enough popular, add recent materials
    if len(popular_materials) < limit:
        recent_materials = StudyMaterial.objects.exclude(
            id__in=[m.id for m in popular_materials]
        ).order_by('-id')[:limit - len(popular_materials)]
        popular_materials.extend(list(recent_materials))
    
    return popular_materials


def track_material_view(user, material, time_spent, completed):
    """
    Track when a student views a study material.
    Used for personalization and analytics.
    
    Args:
        user: The User object
        material: The StudyMaterial object
        time_spent: Time spent viewing in seconds
        completed: Boolean indicating if material was fully completed
    """
    MaterialView.objects.create(
        user=user,
        material=material,
        time_spent=time_spent,
        completion_status=completed
    )


def calculate_impact_score(user, material, before_score, before_total, after_score, after_total):
    """
    Calculate the impact score for a study material.
    Impact Score = After Score % - Before Score %
    
    Labels:
    - Highly Effective: Impact >= 20%
    - Moderate: Impact >= 10%
    - Low: Impact < 10%
    
    Returns:
        Dict with attempt details and calculated impact metrics
    """
    # Calculate percentages for immediate feedback
    before_percentage = (before_score / before_total * 100) if before_total > 0 else 0
    after_percentage = (after_score / after_total * 100) if after_total > 0 else 0
    impact_score = round(after_percentage - before_percentage, 1)
    
    # Determine impact label (boundaries: 10% and 20%)
    if impact_score > 20:
        impact_label = 'Highly Effective'
    elif impact_score > 10:
        impact_label = 'Moderate'
    else:
        impact_label = 'Low'
    
    # Create the attempt record
    attempt = MaterialAttempt.objects.create(
        user=user,
        material=material,
        before_score=before_score,
        before_total=before_total,
        after_score=after_score,
        after_total=after_total,
    )
    
    return {
        'id': attempt.id,
        'impact_score': impact_score,
        'impact_label': impact_label,
        'before_percentage': round(before_percentage, 1),
        'after_percentage': round(after_percentage, 1),
        'material_title': material.title,
        'created_at': attempt.studied_at
    }


def get_material_effectiveness(material):
    """
    Get aggregated effectiveness metrics for a specific material.
    Useful for identifying best-performing materials.
    
    Returns:
        Dict with average impact, usage count, and effectiveness label
    """
    attempts = MaterialAttempt.objects.filter(material=material)
    
    if not attempts.exists():
        return {
            'total_attempts': 0,
            'average_impact': 0,
            'effectiveness_label': 'No Data'
        }
    
    # Calculate average impact
    impacts = []
    for attempt in attempts:
        impacts.append(attempt.impact_score)
    
    avg_impact = sum(impacts) / len(impacts)
    
    # Determine effectiveness (boundaries: 10% and 20%)
    if avg_impact > 20:
        label = 'Highly Effective'
    elif avg_impact > 10:
        label = 'Moderate'
    elif avg_impact > 0:
        label = 'Slightly Effective'
    elif avg_impact == 0:
        label = 'Neutral'
    else:
        label = 'Needs Review'
    
    return {
        'total_attempts': attempts.count(),
        'average_impact': round(avg_impact, 1),
        'effectiveness_label': label,
        'min_impact': min(impacts),
        'max_impact': max(impacts)
    }


def get_personalized_dashboard_data(user):
    """
    Get personalized data for student dashboard.
    Includes weak concepts, recommended materials, and mastery levels.
    
    Enhanced with:
    - New user handling
    - Material effectiveness data
    - Concept-based recommendations
    
    Returns:
        Dict with all dashboard data
    """
    # Check if user has any quiz history
    has_quiz_history = QuizAttempt.objects.filter(user_identifier=user.username).exists()
    
    if not has_quiz_history:
        # New user - return popular/recent materials
        return {
            'weak_topics': [],
            'recommended_materials': [],
            'new_user_recommendations': get_new_user_recommendations(),
            'recent_views': [],
            'impact_data': [],
            'is_new_user': True
        }
    
    # Get weak concepts
    weak_topics = get_weak_concepts(user)
    
    # Get recommended materials
    recommended_materials = get_recommended_materials(user, weak_topics)
    
    # Get recent material views with related data
    recent_views = MaterialView.objects.filter(user=user).select_related('material')[:5]
    
    # Get impact scores for completed materials with calculated scores
    impact_records = MaterialAttempt.objects.filter(user=user).select_related('material')[:5]
    impact_data = []
    for record in impact_records:
        impact_data.append({
            'id': record.id,
            'material_title': record.material.title,
            'impact_score': record.impact_score,
            'impact_label': record.impact_label,
            'before_percentage': (record.before_score / record.before_total * 100) if record.before_total > 0 else 0,
            'after_percentage': (record.after_score / record.after_total * 100) if record.after_total > 0 else 0,
        })
    
    return {
        'weak_topics': weak_topics,
        'recommended_materials': recommended_materials,
        'new_user_recommendations': [],
        'recent_views': recent_views,
        'impact_data': impact_data,
        'is_new_user': False
    }


def get_learning_progress_summary(user):
    """
    Get a comprehensive learning progress summary for the user.
    
    Returns:
        Dict with topics attempted, accuracy trends, and mastery levels
    """
    attempts = QuizAttempt.objects.filter(user_identifier=user.username)
    
    if not attempts.exists():
        return {
            'total_topics_attempted': 0,
            'total_quizzes_taken': 0,
            'overall_accuracy': 0,
            'mastery_levels': {}
        }
    
    # Get unique topics
    topics_attempted = attempts.values('topic__id').distinct().count()
    total_quizzes = attempts.count()
    
    # Calculate overall accuracy
    total_score = sum(a.score for a in attempts)
    total_questions = sum(a.total_questions for a in attempts)
    overall_accuracy = (total_score / total_questions * 100) if total_questions > 0 else 0
    
    # Get mastery levels per topic
    topic_performance = {}
    for attempt in attempts:
        topic_id = attempt.topic_id
        if topic_id:
            if topic_id not in topic_performance:
                topic_performance[topic_id] = {
                    'topic': attempt.topic,
                    'total': 0,
                    'correct': 0
                }
            topic_performance[topic_id]['total'] += attempt.total_questions
            topic_performance[topic_id]['correct'] += attempt.score
    
    mastery_levels = {}
    for topic_id, data in topic_performance.items():
        accuracy = (data['correct'] / data['total'] * 100) if data['total'] > 0 else 0
        if accuracy >= 80:
            mastery_levels[topic_id] = {'level': 'Mastered', 'accuracy': accuracy}
        elif accuracy >= 60:
            mastery_levels[topic_id] = {'level': 'Proficient', 'accuracy': accuracy}
        elif accuracy >= 40:
            mastery_levels[topic_id] = {'level': 'Developing', 'accuracy': accuracy}
        else:
            mastery_levels[topic_id] = {'level': 'Beginning', 'accuracy': accuracy}
    
    return {
        'total_topics_attempted': topics_attempted,
        'total_quizzes_taken': total_quizzes,
        'overall_accuracy': round(overall_accuracy, 1),
        'mastery_levels': mastery_levels
    }
