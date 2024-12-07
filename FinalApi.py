import os
import requests 
import joblib 
import numpy as np 
import cv2 
import json
import re
from flask import Flask, request, jsonify
from skimage.feature import local_binary_pattern 
from skimage.feature import graycomatrix, graycoprops 
import google.generativeai as genai 
from keras.models import load_model  # For soil classification 

# Load the trained models
model1 = joblib.load("npk_ph_predictor_model.pkl")  # NPK prediction model
soil_classifier_model = load_model(
    "keras_model.h5", compile=False)  # Soil classification model

# Initialize Flask app
app = Flask(__name__)

# Configure Gemini AI
genai.configure(api_key="AIzaSyDf6tv5q58fpkRb5aH27UqTiLH8T7ehvw4")

# Define the model and generation configuration
generation_config = {
    "temperature": 1,
    "top_p": 0.95,
    "top_k": 40,
    "max_output_tokens": 8192,
    "response_mime_type": "text/plain",
}

# Create the model instance
model = genai.GenerativeModel(
    model_name="gemini-1.5-flash",
    generation_config=generation_config,
)

# Start a chat session
chat_session = model.start_chat()

# Feature extraction function for NPK prediction


def extract_features_from_image(image):
    image = cv2.resize(image, (224, 224))

    # Compute mean and std for RGB channels
    mean_color = np.mean(image, axis=(0, 1))
    std_color = np.std(image, axis=(0, 1))

    # Compute normalized RGB
    sum_rgb = np.sum(mean_color)
    norm_r = mean_color[2] / sum_rgb
    norm_g = mean_color[1] / sum_rgb
    norm_b = mean_color[0] / sum_rgb

    # Nitrogen indicator (G dominance)
    green_dominance = mean_color[1] > (mean_color[0] + mean_color[2]) / 2

    # Phosphorus indicator (Bluish soil)
    blue_ratio = mean_color[0] / (mean_color[1] + mean_color[2])

    # Potassium: Yellowish-Blue Ratio
    yellowish_blue_ratio = (
        (mean_color[2] + mean_color[1]) / 2) / mean_color[0]

    # Texture Features
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    # Local Binary Pattern (LBP)
    lbp = local_binary_pattern(gray, P=8, R=1, method="uniform")
    lbp_hist, _ = np.histogram(
        lbp.ravel(), bins=np.arange(0, 10), density=True)

    # GLCM (Gray Level Co-occurrence Matrix)
    glcm = graycomatrix(gray, distances=[1], angles=[
                        0], levels=256, symmetric=True, normed=True)
    glcm_contrast = graycoprops(glcm, "contrast")[0, 0]
    glcm_homogeneity = graycoprops(glcm, "homogeneity")[0, 0]
    glcm_entropy = -np.sum(glcm * np.log2(glcm + (glcm == 0)))

    # Combine features
    features = [
        mean_color[2], mean_color[1], mean_color[0],
        std_color[2], std_color[1], std_color[0],
        norm_r, norm_g, norm_b,
        green_dominance, blue_ratio, yellowish_blue_ratio,
        glcm_contrast, glcm_homogeneity, glcm_entropy
    ]
    return features

# Soil classification function


def classify_soil(image):
    # Resize image to match model input
    image_resized = cv2.resize(image, (224, 224))
    image_array = np.expand_dims(image_resized, axis=0)  # Add batch dimension
    prediction = soil_classifier_model.predict(image_array)  # Make prediction

    confidence = np.max(prediction)  # Get the confidence level
    predicted_class = np.argmax(prediction)  # Get the predicted class

    return predicted_class, confidence

# Gemini AI response parser


def parse_gemini_response(response_text):
    try:
        # Extract JSON-like structure from the response
        json_match = re.search(r'{.*}', response_text, re.DOTALL)
        if json_match:
            json_data = json_match.group()
            return json.loads(json_data)
        else:
            raise ValueError("No JSON found in Gemini AI response")
    except json.JSONDecodeError as e:
        print(f"JSONDecodeError: {e}")
        print(f"Raw Gemini Response: {response_text}")
        return {"error": "Invalid JSON format received from Gemini AI"}
    except Exception as e:
        print(f"Unexpected Error: {e}")
        print(f"Raw Gemini Response: {response_text}")
        return {"error": str(e)}

# Function to generate fertilizer recommendations


def get_fertilizer_recommendation(n_value, p_value, k_value, ph_value, crop_type, soil_type, weather):
    prompt = f"""
        Given the following soil and crop information, generate a **fertilizer recommendation** in **only JSON format**:
        
        - Nitrogen (N) level: {n_value}% (Consider values: N<=0.02 => low, 0.02<N<=0.05 => moderate, N>0.05 => high)
        - Phosphorus (P) level: {p_value} ppm (Consider values: P<=6 => low, 6<P<=20 => moderate, P>20 => high)
        - Potassium (K) level: {k_value} ppm (Consider values: K<=50 => low, 50<K<=150 => moderate, K>150 => high)
        - pH level: {ph_value}
        - Crop Type: {crop_type} (This could be crops like wheat, rice, maize, etc.)
        - Soil Type: {soil_type} (other common soil types can be specified)
        - Weather Conditions: {weather} (Can vary depending on region; options could include humid, dry, or temperate)

        Provide the following fertilizer details in JSON:
        {{
            "fertilizer_name": "Common fertilizer name based on soil and crop needs",
            "fertilizer_quantity": "Recommended quantity in kg/hectare",
            "application_schedule": "When to apply (e.g., pre-planting, post-planting)",
            "application_method": "How to apply (e.g., broadcast, side dressing, fertigation)",
            "data": "Other important details regarding fertilizer usage or soil improvement"
        }}
    """
    response = chat_session.send_message(prompt)
    if response and hasattr(response, 'text'):
        result_text = response.text
     #   print(f"Gemini AI Raw Response:\n{result_text}")
        return parse_gemini_response(result_text)
    else:
        return {"error": "No valid response received from Gemini AI"}

# API endpoint for NPK prediction from images


@app.route("/predict_npk", methods=["POST"])
def predict_npk():
    try:
        image_urls = request.json.get("image_urls", [])
        if not image_urls:
            return jsonify({"error": "No image URLs provided."}), 400

        all_features = []
        # List to store (predicted_class, confidence) for each image
        class_confidences = []

        for url in image_urls:
            try:
                response = requests.get(url)
                response.raise_for_status()
                img_array = np.array(
                    bytearray(response.content), dtype=np.uint8)
                image = cv2.imdecode(img_array, cv2.IMREAD_COLOR)

                # Perform soil classification first
                predicted_class, confidence = classify_soil(image)
                class_confidences.append((predicted_class, confidence))

                # Proceed to extract features for NPK analysis if confidence is high enough
                if confidence >= 0.40:
                    features = extract_features_from_image(image)
                    all_features.append(features)
                else:
                    # Skip low-confidence classifications
                    continue

            except requests.exceptions.RequestException as e:
                return jsonify({"error": f"Failed to fetch image from {url}: {str(e)}"}), 400
            except Exception as e:
                return jsonify({"error": f"Error processing image from {url}: {str(e)}"}), 400

        # After processing all images, check if we have any valid classifications
        if not class_confidences:
            return jsonify({"error": "No valid soil classifications with confidence >= 40%."}), 400

        # Find the class with the highest confidence
        highest_confidence_class = max(class_confidences, key=lambda x: x[1])

        # Check if the highest confidence is still below 40%
        if highest_confidence_class[1] < 0.40:
            return jsonify({"error": "Unknown image. Soil classification confidence below 40%."}), 400

        # Proceed with feature extraction for NPK prediction (using valid images)
        avg_features = np.mean(all_features, axis=0) if all_features else None

        if avg_features is not None:
            prediction = model1.predict([avg_features])
            n_value, p_value, k_value, ph_value = prediction[0]
            result = True
            soil_types = {
                0: "Alluvial soil",
                1: "Black soil",
                2: "Chalky soil",
                3: "Clay soil",
                4: "Mary soil",
                5: "Red soil",
                6: "Sand soil",
                7: "Silt soil"
            }

            return jsonify({
                "n_value(%)": n_value,
                "p_value(ppm)": p_value,
                "k_value(ppm)": k_value,
                "ph_value": ph_value,
                "Predicted_Soil_Class": soil_types[highest_confidence_class[0]],
                "Success": result
            })
        else:
            return jsonify({"error": "No valid images with sufficient confidence for NPK prediction."}), 400

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# API endpoint for fertilizer recommendation


@app.route("/recommend_fertilizer", methods=["POST"])
def recommend_fertilizer():
    try:
        data = request.json
        n_value = data.get("n_value")
        p_value = data.get("p_value")
        k_value = data.get("k_value")
        ph_value = data.get("ph_value")
        crop_type = data.get("crop_type", "wheat")
        soil_type = data.get("soil_type", "Alluvial Soil")
        weather = data.get("weather", "Humid")

        if None in [n_value, p_value, k_value, ph_value]:
            return jsonify({"error": "N, P, K, and pH values must be provided."}), 400

        fertilizer_recommendation = get_fertilizer_recommendation(
            n_value, p_value, k_value, ph_value, crop_type, soil_type, weather
        )

        if "fertilizer_name" in fertilizer_recommendation:
            return jsonify({
                "fertilizer_recommendation": fertilizer_recommendation
            })
        else:
            return jsonify({"error": "Failed to generate a valid fertilizer recommendation."}), 500

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Run the Flask app
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
