import time
import functools
import logging
import psutil
import shutil
from typing import Callable, Any

logger = logging.getLogger("Utils")

def retry(max_attempts: int = 3, delay: int = 2, backoff: int = 2, exceptions: tuple = (Exception,)):
    """
    Decorador para reintentar una función en caso de error.
    
    :param max_attempts: Número máximo de intentos.
    :param delay: Segundos de espera inicial entre intentos.
    :param backoff: Multiplicador de delay para cada fallo.
    :param exceptions: Tupla de excepciones a capturar.
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            attempt = 1
            current_delay = delay
            while attempt <= max_attempts:
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    if attempt == max_attempts:
                        logger.error(f"Error final en {func.__name__} después de {max_attempts} intentos: {type(e).__name__}")
                        raise
                    
                    logger.warning(f"Error en {func.__name__} (intento {attempt}/{max_attempts}): {type(e).__name__}. Reintentando en {current_delay}s...")
                    time.sleep(current_delay)
                    current_delay *= backoff
                    attempt += 1
            return None
        return wrapper
    return decorator


class SystemChecker:
    """Utilidad para comprobar recursos del sistema (RAM, SSD)."""
    
    @staticmethod
    def check_memory(min_free_mb: int = 500) -> bool:
        """Verifica que haya al menos min_free_mb de RAM libre."""
        mem = psutil.virtual_memory()
        free_mb = mem.available / (1024 * 1024)
        if free_mb < min_free_mb:
            logger.error(f"Memoria RAM insuficiente. Libre: {free_mb:.2f} MB, Requerido: {min_free_mb} MB")
            return False
        return True
    
    @staticmethod
    def check_disk(min_free_gb: float = 2.0, path: str = ".") -> bool:
        """Verifica que haya al menos min_free_gb de disco libre en el path."""
        usage = shutil.disk_usage(path)
        free_gb = usage.free / (1024**3)
        if free_gb < min_free_gb:
            logger.error(f"Espacio en disco insuficiente. Libre: {free_gb:.2f} GB, Requerido: {min_free_gb} GB")
            return False
        return True
        
    @staticmethod
    def assert_resources(min_ram_mb: int = 500, min_disk_gb: float = 2.0):
        """Lanza excepción si no hay recursos suficientes."""
        if not SystemChecker.check_memory(min_ram_mb):
            raise MemoryError("No hay suficiente RAM disponible para ejecutar la tarea.")
        if not SystemChecker.check_disk(min_disk_gb):
            raise OSError("No hay suficiente espacio en disco disponible.")
            
    @staticmethod
    def print_stats():
        """Imprime estadísticas del sistema."""
        mem = psutil.virtual_memory()
        usage = shutil.disk_usage(".")
        
        print("\n--- Estadísticas del Sistema ---")
        print(f"RAM Total:     {mem.total / (1024**3):.2f} GB")
        print(f"RAM Usada:     {mem.used / (1024**3):.2f} GB ({mem.percent}%)")
        print(f"RAM Disponible: {mem.available / (1024**3):.2f} GB")
        
        print(f"\nSSD Total:     {usage.total / (1024**3):.2f} GB")
        print(f"SSD Usado:     {usage.used / (1024**3):.2f} GB ({100 * usage.used / usage.total:.1f}%)")
        print(f"SSD Libre:     {usage.free / (1024**3):.2f} GB")
        print("--------------------------------\n")
