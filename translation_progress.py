import json
import os
import xml.etree.ElementTree as ET

class ProgressManager:
    def __init__(self, xml_path="Tyranny_Structure.xml", save_path="progress_data.json"):
        self.xml_path = xml_path
        self.save_path = save_path
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