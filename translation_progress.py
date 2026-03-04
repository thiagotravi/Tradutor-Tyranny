import json
import os
import xml.etree.ElementTree as ET


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

        # Garante que a pasta de destino existe para salvar progresso.
        save_dir = os.path.dirname(self.save_path)
        if save_dir:
            os.makedirs(save_dir, exist_ok=True)

        self.progress = self._load_initial_progress()

    def _load_initial_progress(self):
        # Se já existir um progresso salvo, carrega ele
        if os.path.exists(self.save_path):
            with open(self.save_path, 'r') as f:
                return json.load(f)
        
        # Caso contrário, varre o XML pela primeira vez
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

    def update_status(self, file_name, status):
        self.progress[file_name] = status
        with open(self.save_path, 'w') as f:
            json.dump(self.progress, f, indent=4)

    def get_stats(self):
        total = len(self.progress)
        concluidos = sum(1 for v in self.progress.values() if v)
        return total, concluidos, (concluidos/total)*100 if total > 0 else 0