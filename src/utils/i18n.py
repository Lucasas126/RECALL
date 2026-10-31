from __future__ import annotations

from typing import Literal

_SPANISH = {
    "Default LLM model": "Modelo LLM por defecto",
    "Default STT model": "Modelo de transcripción por defecto",
    "Use user default": "Usar el modelo por defecto del usuario",
    "User default": "Por defecto del usuario",
    "Chat override": "Seleccionado para este chat",
    "Audio model": "Modelo de audio",
    "No chat selected": "Ningún chat seleccionado",
    "OpenRouter models": "Modelos de OpenRouter",
    "Speech-to-text models": "Modelos de transcripción",
    "Choose a model for this chat.": "Elige un modelo para este chat.",
    "Choose the user's default LLM model. This does not set a chat override.": "Elige el modelo LLM por defecto del usuario. No establece un modelo propio para el chat.",
    "Choose the user's default STT model. This does not set a chat override.": "Elige el modelo de transcripción por defecto del usuario. No establece un modelo propio para el chat.",
    "This chat now uses the user's default LLM model.": "Este chat vuelve a usar el modelo LLM por defecto del usuario.",
    "This chat now uses the user's default STT model.": "Este chat vuelve a usar el modelo de transcripción por defecto del usuario.",
    "Settings\n\nThinking: {visibility}\nAlerts/errors: {language}\nActive agent: {agent}\nDefault LLM: {llm}\nDefault STT: {stt}\n\nUser defaults are shared with the web. Chats with no selected model inherit them.": "Ajustes\n\nThinking: {visibility}\nAvisos/errores: {language}\nAgente activo: {agent}\nLLM por defecto: {llm}\nTranscripción por defecto: {stt}\n\nLos modelos por defecto se comparten con la web. Los chats sin modelo propio los heredan.",
    "Settings are shared with Telegram. Chats without a selected model use your current defaults.": "Los ajustes se comparten con Telegram. Los chats sin modelo propio usan tus modelos por defecto actuales.",
    "Choosing a model for a chat sets only that chat's override. Use user default removes it. Defaults in Settings are saved with Save settings. Default LLM and STT selections require ZDR. Favorites are shared across your agents.": "Elegir un modelo para un chat solo cambia ese chat. Usar el modelo por defecto del usuario elimina esa selección. Los modelos por defecto de Ajustes se guardan con Guardar ajustes. Las selecciones de LLM por defecto y transcripción requieren ZDR. Los favoritos se comparten entre tus agentes.",
    "✅ STT model selected:\n{model_id}\n\nThis selection applies only to the current chat. ZDR: enabled.": "✅ Modelo de transcripción seleccionado:\n{model_id}\n\nEsta selección solo se aplica al chat actual. ZDR: activado.",
    "The speech-to-text model is not available with the required routing policy.": "El modelo de transcripción no está disponible con la política de rutas requerida.",
    "Could not load settings. Try /settings again.": "No se han podido cargar los ajustes. Prueba /settings de nuevo.",
    "Could not load model favorites: {error}": "No se han podido cargar los modelos favoritos: {error}",
    "Could not load or save settings: {error}\nTry /settings again.": "No se han podido cargar o guardar los ajustes: {error}\nPrueba /settings de nuevo.",
    "Could not create a chat: {error}": "No se ha podido crear un chat: {error}",
    "Could not prepare the chat: {error}": "No se ha podido preparar el chat: {error}",
    "Could not select the agent: {error}": "No se ha podido seleccionar el agente: {error}",
    "Attachment rejected: {error}": "Adjunto rechazado: {error}",
    "Generation failed: {error}": "La generación ha fallado: {error}",
    "No agents are configured for this user.": "Este usuario no tiene agentes configurados.",
    "This agent menu has expired. Open /settings again.": "Este menú de agentes ha caducado. Abre /settings de nuevo.",
    "Unknown settings option. Open /settings again.": "Opción de ajustes desconocida. Abre /settings de nuevo.",
    "Settings\n\nThinking: {visibility}\nAlerts/errors: {language}\nActive agent: {agent}\n\nThinking and language are shared with the web.": "Ajustes\n\nThinking: {visibility}\nAvisos/errores: {language}\nAgente activo: {agent}\n\nThinking e idioma se comparten con la web.",
    "Active agent": "Agente activo",
    "Settings closed.": "Ajustes cerrados.",
    "visible": "visible",
    "hidden": "oculto",
    "None": "Ninguno",
    "Thinking: {visibility}": "Thinking: {visibility}",
    "Back": "Volver",
    "Choose the agent for new chats:": "Elige el agente para los chats nuevos:",
    "New chat created: {chat_id}\nYou can now choose a model or send your first message.": "Chat nuevo creado: {chat_id}\nYa puedes elegir un modelo o enviar tu primer mensaje.",
    "Create a chat first with /newchat.": "Crea primero un chat con /newchat.",
    "No active chat. Use /newchat.": "No hay ningún chat activo. Usa /newchat.",
    "I cannot load the models right now. Please try again in a few seconds.": "No se pueden cargar los modelos ahora. Inténtalo de nuevo en unos segundos.",
    "No ZDR models found. Try /selectmodel followed by part of a model name.": "No se han encontrado modelos ZDR. Prueba /selectmodel seguido de parte del nombre de un modelo.",
    "The session has expired. Use /newchat.": "La sesión ha caducado. Usa /newchat.",
    "That model could not be selected.\nUse /selectmodel to try again.": "No se ha podido seleccionar ese modelo.\nUsa /selectmodel para intentarlo de nuevo.",
    "✅ Model selected\n\n{model_id}\n\nThis selection applies to the current chat.\nZDR: enabled": "✅ Modelo seleccionado\n\n{model_id}\n\nEsta selección se aplica al chat actual.\nZDR: activado",
    "I could not read the current chat model.": "No se ha podido consultar el modelo del chat actual.",
    "Current chat model:\n{model_id}\n\nZDR: {policy}": "Modelo del chat actual:\n{model_id}\n\nZDR: {policy}",
    "enabled": "activado",
    "disabled": "desactivado",
    "Current model: {model}\n\nPage {page}/{pages}. {count} models available.\nChoose another model. Prices are USD per 1 million tokens (input / output). ZDR enabled:": "Modelo actual: {model}\n\nPágina {page}/{pages}. {count} modelos disponibles.\nElige otro modelo. Precios en USD por millón de tokens (entrada / salida). ZDR activado:",
    "I cannot load speech-to-text models right now. Please try again later.": "No se pueden cargar los modelos de transcripción ahora. Inténtalo más tarde.",
    "No ZDR speech-to-text models found. Try /selectstt followed by a search term.": "No se han encontrado modelos ZDR de transcripción. Prueba /selectstt seguido de un término de búsqueda.",
    "Current STT model: {model}\n\nPage {page}/{pages}. {count} ZDR models available.\nChoose a speech-to-text model. Prices are USD per 1 million input tokens:": "Modelo de transcripción actual: {model}\n\nPágina {page}/{pages}. {count} modelos ZDR disponibles.\nElige un modelo de transcripción. Precios en USD por millón de tokens de entrada:",
    "✅ Speech-to-text model selected:\n{model}": "✅ Modelo de transcripción seleccionado:\n{model}",
    "Current speech-to-text model:\n{model}": "Modelo de transcripción actual:\n{model}",
    "✅ STT model selected:\n{model_id}\n\nZDR: enabled. This model will transcribe future audio messages.": "✅ Modelo de transcripción seleccionado:\n{model_id}\n\nZDR: activado. Este modelo transcribirá los próximos mensajes de audio.",
    "Current STT model:\n{model}\n\nZDR: enabled": "Modelo de transcripción actual:\n{model}\n\nZDR: activado",
    "Only image, video and audio files are supported.": "Solo se admiten archivos de imagen, vídeo y audio.",
    "This message does not contain a supported attachment.": "Este mensaje no contiene ningún adjunto compatible.",
    "Telegram attachments must be 22 MB or smaller.": "Los adjuntos de Telegram no pueden superar los 22 MB.",
    "The operation failed. Please try again.": "La operación ha fallado. Inténtalo de nuevo.",
    "I could not load your agents right now.": "No se han podido cargar tus agentes ahora.",
    "Choose the agent for new and incoming messages:": "Elige el agente para los mensajes nuevos y entrantes:",
    "I could not read the current agent.": "No se ha podido consultar el agente actual.",
    "Current agent:\n{name} ({agent_id})": "Agente actual:\n{name} ({agent_id})",
    "✅ Agent selected: {agent_id}\nUse /newchat to create a chat for this agent.": "✅ Agente seleccionado: {agent_id}\nUsa /newchat para crear un chat con este agente.",
    "**_New chat started_**\n\n": "**_Chat nuevo iniciado_**\n\n",
    "\n\n_[Interrupted — your latest message is being processed.]_": "\n\n_[Interrumpido: se está procesando tu último mensaje.]_",
    "_(The model returned no text.)_": "_(El modelo no ha devuelto texto.)_",
    "⬅️ Previous": "⬅️ Anterior",
    "➡️ Next": "➡️ Siguiente",
    "⬅️ Previous STT": "⬅️ Anterior STT",
    "➡️ Next STT": "➡️ Siguiente STT",
    "not logged in": "No has iniciado sesión.",
    "message is empty": "El mensaje está vacío.",
    "model browsing is only available for the OpenRouter chat provider": "La selección de modelos solo está disponible con OpenRouter.",
    "speech-to-text model browsing requires the OpenRouter chat provider": "La selección de modelos de transcripción requiere OpenRouter.",
    "audio messages require an OpenRouter chat provider for speech-to-text": "Los mensajes de audio requieren OpenRouter para la transcripción.",
    "model is not available in the OpenRouter chat catalog with the selected ZDR filter": "El modelo no está disponible en el catálogo de OpenRouter con el filtro ZDR seleccionado.",
    "wait for this agent's active and queued replies before changing models": "Espera a que terminen las respuestas activas y pendientes de este agente antes de cambiar de modelo.",
    "unknown user: {user_id}": "Usuario desconocido: {user_id}",
    "unknown agent: {agent_id}": "Agente desconocido: {agent_id}",
    "unknown chat: {chat_id}": "Chat desconocido: {chat_id}",
    "agent '{agent_id}' is not owned by user '{user_id}'": "El agente '{agent_id}' no pertenece al usuario '{user_id}'.",
    "invalid {kind} id: {value!r}": "Identificador de {kind} no válido: {value!r}",
    "invalid audio data for '{name}'": "Datos de audio no válidos para '{name}'.",
    "Could not read preferences; check the user's preferences.json.": "No se han podido leer los ajustes; comprueba el archivo preferences.json del usuario.",
    "Could not save preferences; check configuration directory permissions and whether another process is holding the preferences lock.": "No se han podido guardar los ajustes; comprueba los permisos del directorio de configuración y si otro proceso mantiene el bloqueo.",
    "Could not read model favorites; check the user's openrouter-favorites.json.": "No se han podido leer los favoritos; comprueba el archivo openrouter-favorites.json del usuario.",
    "Could not save model favorites; check configuration directory permissions.": "No se han podido guardar los favoritos; comprueba los permisos del directorio de configuración.",
    "Could not read model selection for chat {chat_id}.": "No se ha podido leer el modelo seleccionado para el chat {chat_id}.",
    "Could not save model selection for chat {chat_id}.": "No se ha podido guardar el modelo seleccionado para el chat {chat_id}.",
    "Could not reach the OpenRouter catalog; check connectivity and retry.": "No se ha podido acceder al catálogo de OpenRouter; comprueba la conexión e inténtalo de nuevo.",
    "OpenRouter returned an invalid catalog or ZDR policy; retry later.": "OpenRouter ha devuelto un catálogo o una política ZDR no válidos; inténtalo más tarde.",
    "Could not reach the OpenRouter transcription catalog; retry later.": "No se ha podido acceder al catálogo de transcripción de OpenRouter; inténtalo más tarde.",
    "OpenRouter returned an invalid transcription catalog; retry later.": "OpenRouter ha devuelto un catálogo de transcripción no válido; inténtalo más tarde.",
    "OpenRouter catalog request failed (HTTP {status}); retry later.": "La solicitud del catálogo de OpenRouter ha fallado (HTTP {status}); inténtalo más tarde.",
    "OpenRouter transcription catalog request failed (HTTP {status}); retry later.": "La solicitud del catálogo de transcripción de OpenRouter ha fallado (HTTP {status}); inténtalo más tarde.",
    "Delete \"{title}\"?": "¿Eliminar \"{title}\"?",
    "New chat": "Chat nuevo",
    "Catalog unavailable. Use Refresh to retry.": "Catálogo no disponible. Usa Actualizar para intentarlo de nuevo.",
    "Loading current model catalog...": "Cargando el catálogo de modelos...",
    "No models match your search and filters.": "Ningún modelo coincide con la búsqueda y los filtros.",
    "No models match. Try another search or relax the filters.": "Ningún modelo coincide. Prueba otra búsqueda o reduce los filtros.",
    "model": "modelo",
    "models": "modelos",
    "ZDR only": "Solo ZDR",
    "all routes allowed": "Se permiten todas las rutas",
    "Unavailable under the current filter": "No disponible con el filtro actual",
    "Interrupted by a new message": "Interrumpido por un mensaje nuevo",
    "Response stream ended before completion": "La transmisión de la respuesta ha terminado antes de completarse.",
    "Generation changed within a response": "La generación ha cambiado durante una respuesta.",
    "{name}: only image, video and audio files are supported": "{name}: solo se admiten archivos de imagen, vídeo y audio.",
    "{name}: maximum size is 10 MB": "{name}: el tamaño máximo es de 10 MB.",
    "Attachments exceed the 25 MB total limit": "Los adjuntos superan el límite total de 25 MB.",
    "HTTP {status}": "HTTP {status}",
    "Settings": "Ajustes",
    "User settings": "Ajustes de usuario",
    "Close": "Cerrar",
    "Thinking and language are shared with Telegram.": "Thinking e idioma se comparten con Telegram.",
    "Show model thinking": "Mostrar el thinking del modelo",
    "Alerts and errors": "Avisos y errores",
    "Save settings": "Guardar ajustes",
    "ZDR is not required for your next selection. Providers may retain your prompts and replies. Your OpenRouter account may still enforce ZDR.": "Tu próxima selección no exigirá ZDR. Los proveedores pueden conservar tus mensajes y respuestas. Tu cuenta de OpenRouter puede seguir exigiendo ZDR.",
}


def message(locale: str, template: str, **values: object) -> str:
    """Translate a RECALL-owned template, leaving interpolated content untouched."""
    localized = _SPANISH[template] if locale.split("-", 1)[0] == "es" else template
    return localized.format(**values)


def messages(language: Literal["en", "es"]) -> dict[str, str]:
    """Return the public channel translation catalog."""
    return {key: value if language == "es" else key for key, value in _SPANISH.items()}


class UserMessageError(Exception):
    """An error with a translatable RECALL template and opaque interpolation values."""

    def __init__(self, template: str, **values: object) -> None:
        self.template = template
        self.values = values
        super().__init__(template.format(**values) if values else template)


class InputError(UserMessageError, ValueError):
    """Invalid channel or service input."""


def error_message(language: str, error: Exception) -> str:
    """Localize known application errors without translating external error text."""
    if isinstance(error, UserMessageError) and error.template in _SPANISH:
        return message(language, error.template, **error.values)
    return str(error)
