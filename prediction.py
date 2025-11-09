import pandas as pd
import numpy as np
import logging
import warnings
import time
from sklearn.ensemble import HistGradientBoostingRegressor

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
warnings.filterwarnings("ignore", category=UserWarning)

FORECAST_STEPS = 15
MIN_HISTORY_FOR_ML = 300
HISTORY_WINDOW = 10
RETRAIN_INTERVAL_SECONDS = 600
PARAMS = ['iaq', 'voc_index', 'co2_equivalent', 'temperature', 'aqi']

class FastMLForecaster:
    def __init__(self, history_length=HISTORY_WINDOW):
        self.history_length = history_length
        self.models = {}
        self.model_scores = {}

    def _build_features(self, series):
        X, y = [], []
        if len(series) <= self.history_length:
            return np.array(X), np.array(y)
            
        for i in range(len(series) - self.history_length):
            window = series[i:i + self.history_length]
            features = np.concatenate([
                window,
                [np.mean(window)],
                [np.std(window)],
                np.diff(window)
            ])
            X.append(features)
            y.append(series[i + self.history_length])
        return np.array(X), np.array(y)

    def train_model(self, df, param):
        series = df[param].ffill().values
        X, y = self._build_features(series)
        if len(X) < 20:
            return False
            
        model = HistGradientBoostingRegressor(max_iter=100, learning_rate=0.1, max_leaf_nodes=31)
        model.fit(X, y)
        self.models[param] = model
        self.model_scores[param] = model.score(X, y)
        return True

    def predict(self, df, param, steps=FORECAST_STEPS):
        if param not in self.models or len(df) < self.history_length:
            return None
            
        series = df[param].ffill().values
        current_window = series[-self.history_length:].copy()
        forecasts = {}

        for t in range(steps):
            features = np.concatenate([
                current_window,
                [np.mean(current_window)],
                [np.std(current_window)],
                np.diff(current_window)
            ]).reshape(1, -1)
            
            pred = self.models[param].predict(features)[0]
            forecasts[f"step_{t+1}"] = pred
            current_window = np.roll(current_window, -1)
            current_window[-1] = pred
        return forecasts

class HybridForecaster:
    def __init__(self):
        self.ml_forecaster = FastMLForecaster()
        self.ewma_alpha = 0.3
        self.ewma_values = {}
        self.last_retrain_time = {}

    def update_and_predict(self, history_df):
        if history_df.empty:
            return {"status": "No data yet."}
        
        output = {}

        for param in PARAMS:
            if param not in history_df.columns:
                continue

            series = history_df[param].dropna()
            if len(series) < 2:
                output[param] = {"summary": "Analyzing...", "confidence": "Low", "suggestion": ""}
                continue

            last_val = series.iloc[-1]
            prev_val = series.iloc[-2]
            if param not in self.ewma_values:
                self.ewma_values[param] = last_val
            else:
                self.ewma_values[param] = self.ewma_alpha * last_val + (1 - self.ewma_alpha) * self.ewma_values[param]
            
            slope = self.ewma_values[param] - prev_val
            threshold = {'temperature': 0.1, 'iaq': 5.0, 'aqi': 3.0, 'voc_index': 5.0, 'co2_equivalent': 10.0}.get(param, 1.0)
            summary = "Stable"
            if slope > threshold: summary = "Rising"
            elif slope < -threshold: summary = "Decreasing"
            
            suggestion = self._generate_suggestion(param, summary, history_df.iloc[-1])
            output[param] = {"summary": summary, "confidence": "Moderate", "suggestion": suggestion, "forecasts": {}}

            current_time = time.time()
            if len(series) >= MIN_HISTORY_FOR_ML:
                if param not in self.last_retrain_time or (current_time - self.last_retrain_time[param] > RETRAIN_INTERVAL_SECONDS):
                    if self.ml_forecaster.train_model(history_df, param):
                        self.last_retrain_time[param] = current_time

                if param in self.ml_forecaster.models:
                    preds = self.ml_forecaster.predict(history_df, param)
                    if preds:
                        summary = self._interpret_ml_trend(preds, param, series.iloc[-1])
                        suggestion = self._generate_suggestion(param, summary, history_df.iloc[-1])
                        output[param]["summary"] = summary
                        output[param]["suggestion"] = suggestion
                        output[param]["forecasts"] = preds
                        score = self.ml_forecaster.model_scores.get(param, 0)
                        if score > 0.85:
                            output[param]["confidence"] = "High"
                        elif score > 0.7:
                            output[param]["confidence"] = "Moderate"
                        else:
                            output[param]["confidence"] = "Low"
        
        return {"status": "OK", "trends": output}

    def _interpret_ml_trend(self, preds, param, last_value):
        if not preds: return "Analyzing..."
        future_val = preds.get("step_5", last_value)
        
        slope = future_val - last_value
        threshold = {'temperature': 0.2, 'iaq': 10.0, 'aqi': 5.0, 'voc_index': 10.0, 'co2_equivalent': 20.0}.get(param, 2.0)

        if slope > threshold: return "Rising"
        elif slope < -threshold: return "Decreasing"
        return "Stable"
    
    def _generate_suggestion(self, param, summary, latest_data):
        suggestion = ""
        is_iaq_metric = param in ['iaq', 'aqi', 'voc_index']
        if summary == "Rising":
            if is_iaq_metric:
                if latest_data.get('co2_equivalent', 0) > 1200:
                    suggestion = "High CO₂ detected. Consider ventilating."
                elif latest_data.get('humidity', 50) > 70:
                    suggestion = "High humidity may be increasing stuffiness."
                else:
                    suggestion = "Check for indoor pollution sources."
            elif param == 'temperature':
                if latest_data.get('humidity', 50) > 65:
                    suggestion = "High humidity may make it feel warmer."
                else:
                    suggestion = "Room temperature is increasing."
        elif summary == "Decreasing":
            if is_iaq_metric:
                suggestion = "Air quality appears to be improving."
            elif param == 'temperature':
                suggestion = "Room is cooling down."
        return suggestion

hybrid_forecaster_instance = HybridForecaster()

def get_full_prediction(history_buffer_list):
    if not history_buffer_list or len(history_buffer_list) < 2:
        return {"status": "Gathering initial data..."}
    
    df = pd.DataFrame(history_buffer_list)
    return hybrid_forecaster_instance.update_and_predict(df)

