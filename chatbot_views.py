import logging
from django.shortcuts import get_object_or_404
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.decorators import login_required

from mainapp.models import Topic, StudyMaterial
from mainapp.ai_engine import AIEngine

logger = logging.getLogger(__name__)

@login_required
@csrf_exempt
def chatbot_response_api(request):
    """
    API view to handle context-aware chatbot questions.
    Expects POST with 'lesson_id' (Topic ID) and 'question'.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid method'}, status=405)
    
    lesson_id = request.POST.get('lesson_id')
    question = request.POST.get('question')
    
    if not lesson_id or not question:
        return JsonResponse({'error': 'Missing lesson_id or question'}, status=400)
    
    topic = get_object_or_404(Topic, id=lesson_id)
    # Collect content from all study materials for this topic
    materials = StudyMaterial.objects.filter(topic=topic).order_by('order')
    lesson_content = " ".join([m.content_text for m in materials])
    
    if not lesson_content:
        lesson_content = topic.description
        
    try:
        response = AIEngine.generate_chatbot_response(topic.name, lesson_content, question)
        return JsonResponse({'success': True, 'answer': response})
    except Exception as e:
        logger.error(f"Chatbot generation failed: {str(e)}")
        return JsonResponse({'error': f'Failed to generate response: {str(e)}'}, status=500)

@login_required
@csrf_exempt
def simplify_lesson_api(request, lesson_id):
    """
    API view to simplify lesson content.
    lesson_id refers to Topic ID.
    """
    topic = get_object_or_404(Topic, id=lesson_id)
    materials = StudyMaterial.objects.filter(topic=topic).order_by('order')
    lesson_content = " ".join([m.content_text for m in materials])
    
    if not lesson_content:
        return JsonResponse({'error': 'No content found to simplify'}, status=400)
        
    try:
        simplified_text = AIEngine.simplify_content(lesson_content)
        return JsonResponse({'success': True, 'simplified_content': simplified_text})
    except Exception as e:
        logger.error(f"Simplification failed: {str(e)}")
        return JsonResponse({'error': f'Failed to simplify content: {str(e)}'}, status=500)
