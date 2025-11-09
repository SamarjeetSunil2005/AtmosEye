
import pandas as pd
import numpy as np
import os
from datetime import datetime, timedelta
import logging
import iaqcalc

LOG_DIR = "historical_logs"
SESSION_GAP_THRESHOLD_MINUTES = 15

def get_latest_log_file():
    try:
        years = sorted([d for d in os.listdir(LOG_DIR) if os.path.isdir(os.path.join(LOG_DIR, d))], reverse=True)
        if not years: return None
        latest_year = years[0]
        
        months = sorted([d for d in os.listdir(os.path.join(LOG_DIR, latest_year)) if os.path.isdir(os.path.join(LOG_DIR, latest_year, d))], reverse=True)
        if not months: return None
        latest_month = months[0]

        days = sorted([f for f in os.listdir(os.path.join(LOG_DIR, latest_year, latest_month)) if f.endswith(('.csv', '.csv.gz'))], reverse=True)
        if not days: return None
        latest_day_file = days[0]
        
        return os.path.join(LOG_DIR, latest_year, latest_month, latest_day_file)
    except Exception as e:
        logging.error(f"Could not find latest log file: {e}")
        return None

def get_latest_session_data(df):
    # This function now expects a DataFrame with a 'datetime' column
    df = df.sort_values(by='datetime')
    
    time_diffs = df['datetime'].diff().dt.total_seconds() / 60
    
    session_starts = time_diffs[time_diffs > SESSION_GAP_THRESHOLD_MINUTES]
    
    if not session_starts.empty:
        last_session_start_index = session_starts.index[-1]
        return df.loc[last_session_start_index:]
    else:
        return df

def get_iaq_category(iaq_value):
    if iaq_value <= 50: return "good"
    if iaq_value <= 100: return "moderate"
    if iaq_value <= 150: return "unhealthy for sensitive groups"
    if iaq_value <= 200: return "unhealthy"
    if iaq_value <= 300: return "very unhealthy"
    return "hazardous"

def get_stability_trend(std_dev):
    if std_dev < 10: return "stable"
    if std_dev < 25: return "fluctuating"
    return "rapidly changing"

def generate_insight(tone="professional", min_duration_setting=0): # min_duration_setting is ignored
    log_file = get_latest_log_file()
    if not log_file:
        return {"insight": "No historical data found to generate an insight.", "timestamp": datetime.now().isoformat()}

    try:
        read_func = pd.read_csv
        if log_file.endswith('.gz'):
            read_func = lambda f: pd.read_csv(f, compression='gzip')
        
        # FIX 1: Read the CSV, using the first column ('datetime_local') as the index and parse dates
        full_df = read_func(log_file, index_col='datetime_local', parse_dates=True)
        
        # Rename the index to 'datetime' for compatibility with the rest of the script
        full_df.index.name = 'datetime'

    except Exception as e:
        logging.error(f"Could not read log file {log_file}: {e}")
        return {"insight": "Error reading log data.", "timestamp": datetime.now().isoformat()}

    # FIX 2: Pass the dataframe with 'datetime' as a column
    session_df = get_latest_session_data(full_df.reset_index())
    
    if session_df.empty:
         return {"insight": "No recent data session found.", "timestamp": datetime.now().isoformat()}

    runtime_minutes = (session_df['datetime'].iloc[-1] - session_df['datetime'].iloc[0]).total_seconds() / 60

    # FIX 3: Get the duration setting from iaqcalc, not the function argument
    try:
        settings = iaqcalc.get_current_settings()
        min_duration_for_insight = settings.get('insightMinDurationMinutesTotal', 0)
    except Exception as e:
        logging.warning(f"Could not read insight duration setting from iaqcalc: {e}")
        min_duration_for_insight = 0 # Fallback
    
    if runtime_minutes < min_duration_for_insight:
        return {"insight": f"System is calibrating. ({runtime_minutes:.1f} of {min_duration_for_insight} min elapsed). Insights will be available shortly.", "timestamp": datetime.now().isoformat()}

    session_end_time = session_df['datetime'].iloc[-1]
    one_hour_ago = session_end_time - timedelta(hours=1)
    
    # Filter the session_df to the last hour for the *actual insight*
    df = session_df[session_df['datetime'] >= one_hour_ago].copy()
    
    if df.empty:
        df = session_df.copy() # Fallback to whole session if less than 1hr
    
    duration_text = "in the last hour"
    
    avg_iaq = df['iaq'].mean()
    peak_iaq = df['iaq'].max()
    peak_iaq_time_dt = df.loc[df['iaq'].idxmax()]['datetime']
    peak_iaq_time = peak_iaq_time_dt.strftime('%-I:%M %p')
    
    avg_temp = df['temperature'].mean()
    avg_humidity = df['humidity'].mean()
    iaq_std_dev = df['iaq'].std()

    avg_iaq_category = get_iaq_category(avg_iaq)
    peak_iaq_category = get_iaq_category(peak_iaq)
    stability = get_stability_trend(iaq_std_dev)

    summary = ""

    if tone == "scientific":
        summary = (
            f"Analysis for the period '{duration_text}': Mean IAQ was {avg_iaq:.1f} ({avg_iaq_category}), "
            f"peaking at {peak_iaq:.0f} ({peak_iaq_category}) around {peak_iaq_time}. "
            f"Mean temperature was {avg_temp:.1f}°C and humidity was {avg_humidity:.0f}%. "
            f"The environment's IAQ profile showed {stability} patterns."
        )
    elif tone == "friendly":
        if avg_iaq_category == "good":
            main_phrase = "your air has been fresh and healthy"
        elif avg_iaq_category == "moderate":
            main_phrase = "your air quality has been decent"
        else:
            main_phrase = f"your air has been a bit stuffy, averaging in the {avg_iaq_category} range"

        peak_is_old = (session_end_time - peak_iaq_time_dt).total_seconds() > 600 

        if avg_iaq_category == peak_iaq_category or peak_iaq < 100:
            peak_phrase = ""
        elif peak_is_old:
             peak_phrase = f", with a brief spike to {peak_iaq_category} levels around {peak_iaq_time}"
        else:
            peak_phrase = f", but it's currently {peak_iaq_category} (peaked at {peak_iaq_time})"
        
        temp_phrase = f"Temperature and humidity have been comfortable."
        summary = f"Hey there! {main_phrase.capitalize()} {duration_text}{peak_phrase}. {temp_phrase}"
    else: 
        if avg_iaq_category == peak_iaq_category or peak_iaq < 100:
            main_phrase = f"Air quality remained {avg_iaq_category} {duration_text}, with an average IAQ of {avg_iaq:.0f}"
        else:
            main_phrase = (
                f"Air quality was generally {avg_iaq_category} {duration_text}, "
                f"but experienced a spike to {peak_iaq_category} levels (IAQ {peak_iaq:.0f}) around {peak_iaq_time}"
            )

        stability_phrase = f"Environmental conditions were mostly {stability}."
        summary = f"{main_phrase}. {stability_phrase}."

    return {"insight": summary, "timestamp": datetime.now().isoformat()}

#prediction.py

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

