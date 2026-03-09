import json
import os
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path


class ProgressManager:
    def __init__(self, xml_path=None, save_path=None):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        default_xml_path = os.path.join(base_dir, "data", "Tyranny_Structure.xml")
        legacy_xml_path = os.path.join(base_dir, "Tyranny_Structure.xml")
        default_save_path = os.path.join(base_dir, "data", "progress_data.json")
        legacy_save_path = os.path.join(base_dir, "progress_data.json")

        if xml_path:
            self.xml_path = xml_path
        else:
            self.xml_path = default_xml_path if os.path.exists(default_xml_path) else legacy_xml_path

        if save_path:
            self.save_path = save_path
        else:
            self.save_path = default_save_path if os.path.exists(default_save_path) else legacy_save_path
        self.lastgood_path = f"{self.save_path}.lastgood"

        # Garante que a pasta de destino existe para salvar progresso.
        save_dir = os.path.dirname(self.save_path)
        if save_dir:
            os.makedirs(save_dir, exist_ok=True)
        self._io_lock = threading.Lock()

        self.progress, self.audit_items = self._load_initial_progress()

    def _build_progress_from_xml(self):
        progress = {}
        try:
            tree = ET.parse(self.xml_path)
            root = tree.getroot()
            for file_node in root.findall(".//File"):
                file_name = file_node.get("name")
                if file_name:
                    progress[file_name] = False
        except Exception as e:
            print(f"Erro ao carregar XML: {e}")
        return progress

    def _backup_corrupted_progress_file(self):
        src = Path(self.save_path)
        if not src.exists():
            return
        stamp = int(time.time())
        backup = src.with_suffix(src.suffix + f".corrupt-{stamp}.bak")
        try:
            backup.write_bytes(src.read_bytes())
            print(f"Aviso: progresso corrompido detectado. Backup criado em {backup}")
        except Exception as e:
            print(f"Aviso: nao foi possivel criar backup do progresso corrompido: {e}")

    def _load_initial_progress(self):
        # Se já existir um progresso salvo, carrega ele
        if os.path.exists(self.save_path):
            try:
                with open(self.save_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except json.JSONDecodeError:
                # Mantém o app funcional mesmo com JSON parcial/corrompido.
                self._backup_corrupted_progress_file()
                if os.path.exists(self.lastgood_path):
                    try:
                        with open(self.lastgood_path, "r", encoding="utf-8") as f:
                            raw = json.load(f)
                    except Exception:
                        return self._build_progress_from_xml(), []
                else:
                    return self._build_progress_from_xml(), []
            except Exception:
                return self._build_progress_from_xml(), []

            # Novo formato
            if isinstance(raw, dict) and "files" in raw:
                files = raw.get("files", {})
                audit_items = raw.get("audit_items", [])
                if isinstance(files, dict) and isinstance(audit_items, list):
                    normalized_files = {
                        str(k): bool(v) for k, v in files.items()
                    }
                    normalized_audit = [a for a in audit_items if isinstance(a, dict)]
                    return normalized_files, normalized_audit
            # Formato legado: dict simples file->status
            if isinstance(raw, dict):
                normalized_legacy = {
                    str(k): bool(v)
                    for k, v in raw.items()
                    if not str(k).startswith("_") and isinstance(v, (bool, int))
                }
                return normalized_legacy, []

        # Caso contrário, varre o XML pela primeira vez
        return self._build_progress_from_xml(), []

    def _save(self):
        payload = {
            "schema": "v2",
            "files": self.progress,
            "audit_items": self.audit_items,
        }
        tmp_path = f"{self.save_path}.tmp"
        tmp_lastgood = f"{self.lastgood_path}.tmp"
        with self._io_lock:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=4, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.save_path)
            with open(tmp_lastgood, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=4, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_lastgood, self.lastgood_path)

    def update_status(self, file_name, status):
        self.progress[file_name] = status
        self._save()

    def reset_all_status(self):
        for k in list(self.progress.keys()):
            self.progress[k] = False
        self.audit_items = []
        self._save()

    def add_or_update_audit_item(self, item):
        file_name = item.get("file")
        entry_idx = item.get("entry_idx")
        if file_name is None or entry_idx is None:
            return
        for i, existing in enumerate(self.audit_items):
            if existing.get("file") == file_name and existing.get("entry_idx") == entry_idx:
                self.audit_items[i] = item
                self._save()
                return
        self.audit_items.append(item)
        self._save()

    def clear_audit_item(self, file_name, entry_idx):
        before = len(self.audit_items)
        self.audit_items = [
            a for a in self.audit_items
            if not (a.get("file") == file_name and a.get("entry_idx") == entry_idx)
        ]
        if len(self.audit_items) != before:
            self._save()

    def get_audit_items(self, file_name=None):
        if not file_name:
            return list(self.audit_items)
        return [a for a in self.audit_items if a.get("file") == file_name]

    def get_stats(self):
        total = len(self.progress)
        concluidos = sum(1 for v in self.progress.values() if v)
        return total, concluidos, (concluidos/total)*100 if total > 0 else 0

    def rebuild_from_discovered_files(self, file_paths: list[str], source_en_root: str):
        """
        Reconstrói a lista de progresso a partir dos arquivos descobertos no EN,
        preservando status já concluídos mesmo que chaves antigas tenham formato distinto.
        """
        if not file_paths:
            return

        known = {str(k): bool(v) for k, v in self.progress.items()}

        def _resolve_old_status(abs_path: str, rel_key: str):
            if rel_key in known:
                return known[rel_key]
            if abs_path in known:
                return known[abs_path]
            rel_low = rel_key.lower()
            # Caso legado: chave antiga com prefixos de pasta diferentes.
            suffix_matches = [v for k, v in known.items() if k.lower().endswith(rel_low)]
            if len(suffix_matches) == 1:
                return suffix_matches[0]
            contained_matches = [v for k, v in known.items() if rel_low.endswith(k.lower())]
            if len(contained_matches) == 1:
                return contained_matches[0]
            return False

        source_root = Path(source_en_root).expanduser() if source_en_root else None
        rebuilt = {}
        valid_keys = set()
        for p in file_paths:
            abs_path = str(Path(p))
            if source_root:
                try:
                    rel_key = str(Path(p).relative_to(source_root))
                except Exception:
                    rel_key = abs_path
            else:
                rel_key = abs_path
            rebuilt[rel_key] = _resolve_old_status(abs_path, rel_key)
            valid_keys.add(rel_key)

        self.progress = rebuilt
        # Limpa itens de auditoria órfãos para manter consistência com a lista atual.
        self.audit_items = [a for a in self.audit_items if str(a.get("file", "")) in valid_keys]
        self._save()